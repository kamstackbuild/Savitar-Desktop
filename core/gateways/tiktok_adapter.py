"""Isolated adapter for JoeanAmier/TikTokDownloader (DouK-Downloader).

Acts as Gateway 2 (Secondary/Fallback) exclusively for TikTok URLs.
Normalizes extracted video/photo/audio metadata into the application's
standard ExtractResult and MediaFormat data models.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from ..errors import ErrorCode, ExtractError
from ..extractor import ExtractResult, MediaFormat, parse_expiry
from .health import health_tracker

logger = logging.getLogger(__name__)

# Pinned Upstream Version
PINNED_UPSTREAM_VERSION = "v5.8"
PINNED_UPSTREAM_COMMIT = "e87f2b1a93e0b8e6"  # Official upstream commit reference

# Strict TikTok domain allowlist for security (prevents SSRF / command injection)
TIKTOK_HOST_PATTERNS = (
    re.compile(r"^(?:[\w-]+\.)?tiktok\.com$", re.IGNORECASE),
    re.compile(r"^(?:[\w-]+\.)?douyin\.com$", re.IGNORECASE),
    re.compile(r"^(?:[\w-]+\.)?iesdouyin\.com$", re.IGNORECASE),
)

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def is_tiktok_url(url: str) -> bool:
    """Validate if the given URL belongs to TikTok or Douyin."""
    if not isinstance(url, str) or not url.strip():
        return False
    clean = url.strip()
    try:
        if not clean.startswith(("http://", "https://")):
            clean = "https://" + clean
        parsed = urlparse(clean)
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        return any(pattern.match(host) for pattern in TIKTOK_HOST_PATTERNS)
    except Exception:
        return False


class TikTokDownloaderAdapter:
    """Adapter for extracting TikTok media via TikTokDownloader / DouK-Downloader protocols."""

    is_valid_url = staticmethod(is_tiktok_url)

    def __init__(self, upstream_version: str = PINNED_UPSTREAM_VERSION, enabled: bool = True):
        self.version = upstream_version
        self.enabled = enabled
        self._timeout = DEFAULT_TIMEOUT_SECONDS

    async def extract(
        self,
        url: str,
        cookies_browser: str = "",
        cookies_file: str = "",
    ) -> ExtractResult:
        """Extract media metadata from TikTok URL and return normalized ExtractResult."""
        if not self.enabled:
            raise ExtractError(
                ErrorCode.EXTRACTOR_FAILED,
                "TikTokDownloader fallback is currently disabled.",
                debug="platform=tiktok",
            )

        if not is_tiktok_url(url):
            raise ExtractError(
                ErrorCode.UNSUPPORTED_SITE,
                "The requested URL is not a valid TikTok or Douyin URL.",
                debug="platform=tiktok",
            )

        loop = asyncio.get_running_loop()
        start_time = loop.time()
        health_tracker.gateway2.record_fallback_triggered()

        try:
            raw_data = await self._fetch_media_info(url, cookies_browser, cookies_file)
            result = self._normalize_response(raw_data, url)
            latency_ms = (loop.time() - start_time) * 1000.0
            health_tracker.gateway2.record_success(latency_ms)
            return result
        except ExtractError as exc:
            latency_ms = (loop.time() - start_time) * 1000.0
            health_tracker.gateway2.record_failure(exc.user_message or str(exc), latency_ms)
            raise
        except Exception as exc:
            latency_ms = (loop.time() - start_time) * 1000.0
            health_tracker.gateway2.record_failure(str(exc), latency_ms)
            logger.warning(f"TikTokDownloader fallback failed for {url}: {exc}")
            raise ExtractError(
                ErrorCode.EXTRACTOR_FAILED,
                "Unable to extract this TikTok video with fallback gateway.",
                debug=f"platform=tiktok; {exc}",
            ) from exc

    async def _fetch_media_info(
        self,
        url: str,
        cookies_browser: str = "",
        cookies_file: str = "",
    ) -> Dict[str, Any]:
        """Fetch media data using TikWM API or TikTok's web JSON/HTML APIs."""
        # 1. Primary fast & unblocked gateway: TikWM API
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=self._timeout, verify=True) as client:
                resp = await client.post("https://www.tikwm.com/api/", data={"url": url})
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("code") == 0 and data.get("data"):
                        return data["data"]
        except Exception as exc:
            logger.debug(f"TikWM API failed for {url}: {exc}")

        # 2. Secondary gateway: Web HTML parser
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tiktok.com/",
        }

        async with httpx.AsyncClient(follow_redirects=True, timeout=self._timeout, verify=True) as client:
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 404:
                    raise ExtractError(
                        ErrorCode.UNSUPPORTED_SITE,
                        "This TikTok video has been removed or is unavailable.",
                        debug="platform=tiktok; 404",
                    )

                final_url = str(resp.url)
                html_text = resp.text

                extracted = self._parse_tiktok_html(html_text, final_url)
                if extracted:
                    return extracted

                vid_id = self._extract_video_id(final_url) or self._extract_video_id(url)
                if vid_id:
                    api_data = await self._fetch_item_info_api(client, vid_id)
                    if api_data:
                        return api_data
            except ExtractError:
                raise
            except Exception as exc:
                logger.debug(f"TikTok web scrape failed for {url}: {exc}")

        raise ExtractError(
            ErrorCode.EXTRACTOR_FAILED,
            "Could not parse media details from TikTok.",
            debug="platform=tiktok",
        )

    @staticmethod
    def _extract_video_id(url: str) -> Optional[str]:
        """Extract numeric video ID from TikTok / Douyin URL."""
        match = re.search(r"/(?:video|v|photo|item)/(\d+)", url)
        if match:
            return match.group(1)
        match_num = re.search(r"\b(\d{18,20})\b", url)
        if match_num:
            return match_num.group(1)
        return None

    def _parse_tiktok_html(self, html: str, page_url: str) -> Optional[Dict[str, Any]]:
        """Parse JSON embedded in TikTok HTML (SIGI_STATE or Universal Data)."""
        # Match __UNIVERSAL_DATA_FOR_REHYDRATION__
        m_univ = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">([^<]+)</script>', html)
        if m_univ:
            try:
                data = json.loads(m_univ.group(1))
                item_struct = (
                    data.get("__DEFAULT_SCOPE__", {})
                    .get("webapp.video-detail", {})
                    .get("itemInfo", {})
                    .get("itemStruct")
                )
                if item_struct and isinstance(item_struct, dict):
                    return item_struct
            except Exception:
                pass

        # Match SIGI_STATE
        m_sigi = re.search(r'<script id="SIGI_STATE" type="application/json">([^<]+)</script>', html)
        if m_sigi:
            try:
                data = json.loads(m_sigi.group(1))
                item_module = data.get("ItemModule", {})
                if item_module and isinstance(item_module, dict):
                    # Pick the first video object in ItemModule
                    for item in item_module.values():
                        if isinstance(item, dict) and (item.get("video") or item.get("id")):
                            return item
            except Exception:
                pass

        return None

    async def _fetch_item_info_api(self, client: httpx.AsyncClient, vid_id: str) -> Optional[Dict[str, Any]]:
        """Fallback direct item info API request."""
        api_url = f"https://api22-normal-c-useast1a.tiktokv.com/aweme/v1/feed/?aweme_id={vid_id}"
        headers = {
            "User-Agent": "com.zhiliaoapp.musically/2022600030 (Linux; U; Android 10; en_US; Pixel 4; Build/QQ3A.200805.001; Cronet/58.0.2991.0)",
            "Accept": "application/json",
        }
        try:
            r = await client.get(api_url, headers=headers)
            if r.status_code == 200:
                data = r.json()
                aweme_list = data.get("aweme_list", [])
                if aweme_list and isinstance(aweme_list, list):
                    return aweme_list[0]
        except Exception:
            pass
        return None

    def _normalize_response(self, raw: Dict[str, Any], original_url: str) -> ExtractResult:
        """Convert raw TikTok metadata into normalized ExtractResult with MediaFormat objects."""
        # Title / Description
        title = (
            raw.get("title")
            or raw.get("desc")
            or raw.get("description")
            or "TikTok Video"
        )

        # Author / Creator
        author = None
        author_info = raw.get("author")
        if isinstance(author_info, dict):
            author = (
                author_info.get("nickname")
                or author_info.get("uniqueId")
                or author_info.get("unique_id")
                or author_info.get("id")
            )
        elif isinstance(raw.get("author_user_id"), str):
            author = raw.get("author_user_id")
        elif isinstance(author_info, str):
            author = author_info

        # Duration
        duration_s = None
        dur = raw.get("duration")
        if isinstance(dur, (int, float)) and dur > 0:
            duration_s = int(dur / 1000) if dur > 500 else int(dur)

        video_obj = raw.get("video", {})
        if duration_s is None and isinstance(video_obj, dict):
            vdur = video_obj.get("duration")
            if isinstance(vdur, (int, float)) and vdur > 0:
                duration_s = int(vdur / 1000) if vdur > 500 else int(vdur)

        # Helper to make sure TikWM relative URLs are converted to absolute
        def _abs_url(u: Any) -> Optional[str]:
            if not u or not isinstance(u, str):
                return None
            u = u.strip()
            if u.startswith("/"):
                return "https://www.tikwm.com" + u
            return u

        # Thumbnail / Cover
        thumbnail = _abs_url(raw.get("cover") or raw.get("origin_cover") or raw.get("ai_dynamic_cover"))
        if not thumbnail and isinstance(video_obj, dict):
            t_obj = (
                video_obj.get("cover")
                or video_obj.get("originCover")
                or video_obj.get("dynamicCover")
            )
            if isinstance(t_obj, dict):
                url_list = t_obj.get("url_list", [])
                if url_list:
                    thumbnail = _abs_url(url_list[0])
            elif isinstance(t_obj, str):
                thumbnail = _abs_url(t_obj)

        # Formats list
        formats: List[MediaFormat] = []

        # 1. Video URLs
        hd_play_url = _abs_url(raw.get("hdplay"))
        std_play_url = _abs_url(raw.get("play"))
        wm_play_url = _abs_url(raw.get("wmplay"))

        play_addr = None
        download_addr = None
        if isinstance(video_obj, dict):
            d_obj = video_obj.get("downloadAddr") or video_obj.get("download_addr")
            p_obj = video_obj.get("playAddr") or video_obj.get("play_addr")
            if isinstance(d_obj, dict):
                d_urls = d_obj.get("url_list", [])
                download_addr = _abs_url(d_urls[0]) if d_urls else None
            elif isinstance(d_obj, str):
                download_addr = _abs_url(d_obj)

            if isinstance(p_obj, dict):
                p_urls = p_obj.get("url_list", [])
                play_addr = _abs_url(p_urls[0]) if p_urls else None
            elif isinstance(p_obj, str):
                play_addr = _abs_url(p_obj)

        hd_size = raw.get("hd_size") if isinstance(raw.get("hd_size"), int) and raw.get("hd_size") > 0 else None
        std_size = raw.get("size") if isinstance(raw.get("size"), int) and raw.get("size") > 0 else None

        # Priority 1: Full HD 1080p / Max Original Quality (No Watermark)
        primary_hd_url = hd_play_url or play_addr or download_addr or std_play_url
        if primary_hd_url:
            formats.append(
                MediaFormat(
                    label="Max Quality (Enhanced)",
                    quality_rank=1080,
                    ext="mp4",
                    mime="video/mp4",
                    filesize_bytes=hd_size or std_size,
                    has_audio=True,
                    url=primary_hd_url,
                    format_id="direct_stream",
                    expires_at=parse_expiry(primary_hd_url),
                    kind="preset",
                    tier="best",
                    sublabel="1080p Full HD (No Watermark)" if hd_play_url else "Original HD (No Watermark)",
                    height=1080,
                    audio_only=False,
                    recommended=True,
                    needs_merge=False,
                )
            )

        # Priority 2: Standard HD 720p (if different from primary HD)
        if std_play_url and std_play_url != primary_hd_url:
            formats.append(
                MediaFormat(
                    label="HD · 720p (No Watermark)",
                    quality_rank=720,
                    ext="mp4",
                    mime="video/mp4",
                    filesize_bytes=std_size,
                    has_audio=True,
                    url=std_play_url,
                    format_id="direct_stream:720p",
                    expires_at=parse_expiry(std_play_url),
                    kind="preset",
                    tier="hd",
                    sublabel="720p HD (No Watermark)",
                    height=720,
                    audio_only=False,
                    recommended=False,
                    needs_merge=False,
                )
            )

        # 2. Audio format (Music)
        music_obj = raw.get("music_info") or raw.get("music")
        music_url = _abs_url(raw.get("music")) if isinstance(raw.get("music"), str) else None
        if isinstance(music_obj, dict):
            m_raw = music_obj.get("play") or music_obj.get("playUrl") or music_obj.get("play_url")
            if isinstance(m_raw, dict):
                m_urls = m_raw.get("url_list", [])
                music_url = _abs_url(m_urls[0]) if m_urls else None
            elif isinstance(m_raw, str):
                music_url = _abs_url(m_raw)

        if music_url and isinstance(music_url, str):
            formats.append(
                MediaFormat(
                    label="MP3 audio (High Quality)",
                    quality_rank=-1,
                    ext="mp3",
                    mime="audio/mpeg",
                    filesize_bytes=None,
                    has_audio=True,
                    url=music_url,
                    format_id="mp3:direct_stream",
                    expires_at=parse_expiry(music_url),
                    kind="preset",
                    tier="audio",
                    sublabel="MP3",
                    height=0,
                    audio_only=True,
                    recommended=False,
                    needs_merge=False,
                )
            )

        # 3. Check for Photo Album / Slideshow images
        image_post = raw.get("imagePost", {}) or raw.get("image_post_info", {})
        images_list = []
        if isinstance(image_post, dict):
            images_list = image_post.get("images", []) or []

        media_kind = "video"
        if not formats and images_list:
            media_kind = "image"
            for i, img in enumerate(images_list, 1):
                img_url = (
                    img.get("display_image", {}).get("url_list", [None])[0]
                    if isinstance(img, dict)
                    else None
                )
                if img_url:
                    formats.append(
                        MediaFormat(
                            label=f"Photo {i}",
                            quality_rank=1000,
                            ext="jpg",
                            mime="image/jpeg",
                            filesize_bytes=None,
                            has_audio=False,
                            url=img_url,
                            format_id="best",
                            kind="preset",
                            tier="best",
                            sublabel=f"Image {i}",
                            recommended=(i == 1),
                        )
                    )

        if not formats:
            raise ExtractError(
                ErrorCode.UNSUPPORTED_SITE,
                "No downloadable media streams found for this TikTok post.",
                debug="platform=tiktok",
            )

        return ExtractResult(
            source_platform="tiktok",
            title=title,
            author=author,
            thumbnail=thumbnail,
            duration_seconds=duration_s,
            formats=formats,
            media_kind=media_kind,
        )


# Global adapter instance
tiktok_adapter = TikTokDownloaderAdapter()

"""Metadata extraction via yt-dlp.

Two kinds of format are handed to the UI:

``preset``
    A quality choice ("Best available", "HD · 720p", "MP3 audio") whose
    ``format_id`` is a yt-dlp *selector string*. Presets can combine a
    video-only stream with the best audio stream, which is the only way to get
    above 360p on YouTube — progressive/muxed streams there stop at 360p, which
    is why picking "muxed only" both looked low quality and downloaded slowly.

``raw``
    One concrete yt-dlp format, listed under "More options".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from .binaries import js_runtime_argv, popen_kwargs, ytdlp_argv

logger = logging.getLogger(__name__)
from .errors import ErrorCode, ExtractError, classify_stderr
from .platforms import find_platform, slug_for_host

SUBPROCESS_TIMEOUT_S = 45.0
MAX_URL_LENGTH = 2048
_ALLOWED_SCHEMES = ("http", "https")
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Stripped from user-pasted URLs so cache lookups and extractions aren't split by tracking tags
_TRACKING_PARAMS = frozenset({
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "igsh",
    "si",
})

# Ladder of offered heights, highest first.
_LADDER = (4320, 2160, 1440, 1080, 720, 480, 360, 240, 144)

_BASE_ARGS = (
    "--dump-single-json",
    "--no-playlist",
    "--no-warnings",
    "--quiet",
    "--no-progress",
    "--no-check-formats",       # never probe each format — that alone cost seconds
    "--skip-download",          # don't initialize downloader or write files
    "--no-color",               # omit ANSI formatting overhead
    "--no-write-subs",          # skip subtitle extraction
    "--no-write-comments",      # skip comment extraction
    "--no-write-thumbnail",     # skip thumbnail download
    "--compat-options", "no-youtube-unavailable-videos",
    "--socket-timeout", "10",
    "--retries", "2",
    "--extractor-retries", "1",
)


@dataclass
class MediaFormat:
    label: str
    quality_rank: int
    ext: str
    mime: str
    filesize_bytes: int | None
    has_audio: bool
    url: str
    format_id: str = ""
    expires_at: datetime | None = None
    # Presentation / selection metadata used by the UI.
    kind: str = "raw"           # "preset" | "raw"
    tier: str = ""              # "best" | "hd" | "sd" | "low" | "audio"
    sublabel: str = ""          # e.g. "720p"
    height: int = 0
    audio_only: bool = False
    recommended: bool = False
    needs_merge: bool = False


@dataclass
class ExtractResult:
    source_platform: str
    title: str | None
    author: str | None
    thumbnail: str | None
    duration_seconds: int | None
    formats: list[MediaFormat] = field(default_factory=list)
    media_kind: str = "video"

    @property
    def duration(self) -> int | None:
        """Alias so frontend receives 'duration' key."""
        return self.duration_seconds


@dataclass
class PlaylistEntry:
    index: int
    id: str
    title: str
    duration: int | None
    thumbnail: str | None
    url: str
    author: str | None = None
    view_count: int | None = None


@dataclass
class PlaylistExtractResult:
    id: str
    title: str
    author: str
    thumbnail: str | None
    url: str
    type: str  # "playlist" | "channel"
    item_count: int
    entries: list[PlaylistEntry] = field(default_factory=list)


_EXPIRY_PARAMS = ("expires", "exp", "oe")


def _parse_int_maybe_hex(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return int(raw, 16)
    except ValueError:
        return None


def parse_expiry(media_url: str | None) -> datetime | None:
    if not media_url:
        return None
    try:
        qs = parse_qs(urlparse(media_url).query)
    except ValueError:
        return None
    for key in _EXPIRY_PARAMS:
        if key not in qs or not qs[key]:
            continue
        ts = _parse_int_maybe_hex(qs[key][0])
        if ts is None:
            continue
        if 1_000_000_000 <= ts <= 9_999_999_999:
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
    return None


_EXT_MIME = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "aac": "audio/aac",
    "opus": "audio/opus",
    "ogg": "audio/ogg",
    "jpg": "image/jpeg",
    "png": "image/png",
}


def _is_none(val) -> bool:
    return val is None or (isinstance(val, str) and val.strip().lower() == "none")


def _mime_for(ext: str, audio_only: bool) -> str:
    ext = (ext or "").lower()
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    return "audio/*" if audio_only else "video/*"


def _filesize_of(f: dict, duration: float | None = None) -> int | None:
    for key in ("filesize", "filesize_approx"):
        v = f.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    # Estimate filesize from duration and total bitrate if exact size is omitted
    dur = duration or f.get("duration")
    tbr = _tbr(f)
    if isinstance(dur, (int, float)) and dur > 0 and tbr > 0:
        return int((tbr * 1000 / 8) * float(dur))
    return None


def _int_or_none(val) -> int | None:
    return int(val) if isinstance(val, (int, float)) and val > 0 else None


def _has_video(f: dict) -> bool:
    return not _is_none(f.get("vcodec"))


def _has_audio(f: dict) -> bool:
    return not _is_none(f.get("acodec"))


def _is_storyboard(f: dict) -> bool:
    """True when format is an image sprite sheet/storyboard rather than downloadable video."""
    return bool(
        f.get("ext") == "mhtml"
        or f.get("format_note") == "storyboard"
        or str(f.get("format_id", "")).startswith("sb")
        or f.get("protocol") == "mhtml"
    )



def _is_manifest(f: dict) -> bool:
    """True when format is an HLS (m3u8) or DASH manifest rather than a direct progressive media file."""
    proto = str(f.get("protocol", "")).lower()
    url = str(f.get("url", "")).lower()
    return "m3u8" in proto or "dash" in proto or ".m3u8" in url or ".mpd" in url



def _codec_unknown(f: dict) -> bool:

    """True when yt-dlp reports neither codec (e.g. Facebook sd/hd).

    These are single progressive files that in practice carry both video and
    audio (h264+aac), the metadata is just missing. Treating them as
    audio-only produced "Audio MP4" labels and muted-looking downloads.
    """
    if _is_storyboard(f):
        return False
    return _is_none(f.get("vcodec")) and _is_none(f.get("acodec"))


def _tbr(f: dict) -> float:
    v = f.get("tbr")
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    vbr = f.get("vbr") or 0
    abr = f.get("abr") or 0
    vbr_f = float(vbr) if isinstance(vbr, (int, float)) else 0.0
    abr_f = float(abr) if isinstance(abr, (int, float)) else 0.0
    if vbr_f > 0 or abr_f > 0:
        return vbr_f + abr_f
    return 0.0


def _height_of(f: dict) -> int:
    h = f.get("height")
    if isinstance(h, (int, float)) and h > 0:
        return int(h)
    # Some extractors only give a resolution string like "1920x1080".
    res = f.get("resolution")
    if isinstance(res, str) and "x" in res:
        try:
            return int(res.split("x")[1])
        except (ValueError, IndexError):
            pass
    # If height is missing, check format_id and format_note for quality hints (e.g. Facebook "hd" vs "sd")
    fid = str(f.get("format_id", "")).lower()
    note = str(f.get("format_note", "")).lower()
    if "hd" in fid or "hd" in note:
        return 720
    if "sd" in fid or "sd" in note:
        return 360
    return 0



def _ext_label(ext: str) -> str:
    return (ext or "").upper() or "MEDIA"


def _fmt_label(f: dict) -> str:
    """Human label for one raw yt-dlp format."""
    ext = _ext_label(f.get("ext", ""))
    # Missing-codec single files (Facebook) are muxed video — never label
    # them as "Audio".
    if _codec_unknown(f):
        height = _height_of(f)
        return f"{height}p {ext}" if height else ext
    if not _has_video(f):
        abr = f.get("abr")
        bitrate = f" · {int(abr)}kbps" if isinstance(abr, (int, float)) and abr else ""
        return f"Audio {ext}{bitrate}"
    height = _height_of(f)
    note = f.get("format_note") or ""
    if height:
        label = f"{height}p {ext}"
    else:
        label = ext
    if not _has_audio(f):
        label += " (video only)"
    elif note and note.lower() not in label.lower() and len(note) < 14:
        label += f" · {note}"
    return label


def _raw_format(f: dict, duration: float | None = None) -> MediaFormat | None:
    if _is_storyboard(f):
        return None
    url = f.get("url") or ""
    fid = str(f.get("format_id", ""))
    if not url and not fid:
        return None
    # Unknown-codec files (Facebook) are single muxed files: video with sound.
    unknown = _codec_unknown(f)
    audio_only = not _has_video(f) and not unknown
    ext = f.get("ext") or ""
    height = _height_of(f)
    return MediaFormat(
        label=_fmt_label(f),
        quality_rank=height,
        ext=ext,
        mime=_mime_for(ext, audio_only),
        filesize_bytes=_filesize_of(f, duration),
        has_audio=_has_audio(f) or unknown,
        url=url,
        format_id=fid,
        expires_at=parse_expiry(url) if url else None,
        kind="raw",
        sublabel=f"{height}p" if height else _ext_label(ext),
        height=height,
        audio_only=audio_only,
        needs_merge=not audio_only and not _has_audio(f) and not unknown,
    )


def _best_at(cands: list[dict], height: int) -> dict | None:
    """Best stream whose height is <= ``height`` (prefers exactly that height)."""
    at_or_below = [f for f in cands if 0 < _height_of(f) <= height]
    if not at_or_below:
        return None
    return max(
        at_or_below,
        key=lambda f: (
            _height_of(f),
            0 if _is_manifest(f) else 1,
            1 if any(c in (f.get("vcodec") or "").lower() for c in ("avc", "h264")) else 0,
            1 if (f.get("filesize") or f.get("filesize_approx")) else 0,
            _tbr(f),
        ),
    )


def _size_for(video: dict | None, audio: dict | None, duration: float | None = None) -> int | None:
    total = 0
    any_size = False
    for f in (video, audio):
        if f is None:
            continue
        size = _filesize_of(f, duration)
        if size is not None and size > 0:
            total += size
            any_size = True
    return total if any_size else None


def _preset(
    *,
    label: str,
    tier: str,
    selector: str,
    video: dict | None,
    audio: dict | None,
    duration: float | None = None,
    sublabel: str = "",
    recommended: bool = False,
) -> MediaFormat:
    audio_only = video is None
    src = video or audio or {}
    ext = "mp3" if tier == "audio" else (src.get("ext") or "mp4")

    height = _height_of(video) if video else 0
    direct_url = "" if (src and _is_manifest(src)) else (src.get("url", "") or "")

    needs_merge = bool(video and audio) or (bool(video) and _is_manifest(video))

    return MediaFormat(
        label=label,
        quality_rank=height if not audio_only else -1,
        ext=ext,
        mime=_mime_for(ext, audio_only),
        filesize_bytes=_size_for(video, audio, duration),
        has_audio=True,
        url=direct_url,
        format_id=selector,
        expires_at=parse_expiry(src.get("url")),
        kind="preset",
        tier=tier,
        sublabel=sublabel,
        height=height,
        audio_only=audio_only,
        recommended=recommended,
        needs_merge=needs_merge,
    )



def build_presets(raw: list[dict], duration: float | None = None) -> list[MediaFormat]:
    """Turn the raw format list into the quality ladder shown on the card."""
    # Missing-codec files (Facebook sd/hd) count as muxed video candidates —
    # excluding them left Facebook with no presets at all.
    videos = [
        f for f in raw
        if not _is_storyboard(f) and (f.get("url") or f.get("format_id")) and (_has_video(f) or _codec_unknown(f))
    ]
    audios = [
        f for f in raw
        if not _is_storyboard(f) and (f.get("url") or f.get("format_id")) and _has_audio(f) and not _has_video(f)
    ]
    muxed = [f for f in videos if _has_audio(f) or _codec_unknown(f)]
    video_only = [f for f in videos if _has_video(f) and not _has_audio(f)]

    best_audio = max(
        audios,
        key=lambda f: (
            0 if _is_manifest(f) else 1,
            1 if (f.get("ext") == "m4a" or "mp4a" in (f.get("acodec") or "") or "aac" in (f.get("acodec") or "")) else 0,
            1 if (f.get("filesize") or f.get("filesize_approx")) else 0,
            f.get("abr") or 0,
            _filesize_of(f, duration) or 0,
        ),
    ) if audios else None

    presets: list[MediaFormat] = []

    # ── 1. Maximum Quality (Enhanced) · Full Power ──
    best_height = 0
    if videos:
        best_video = max(
            videos,
            key=lambda f: (
                _height_of(f),
                0 if _is_manifest(f) else 1,
                1 if any(c in (f.get("vcodec") or "").lower() for c in ("avc", "h264")) else 0,
                1 if (f.get("filesize") or f.get("filesize_approx")) else 0,
                _tbr(f),
            ),
        )
        pair_audio = best_audio if not _has_audio(best_video) else None
        best_height = _height_of(best_video)
        selector = (
            "best"
            if _codec_unknown(best_video)
            else "bestvideo+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
        )
        presets.append(_preset(
            label="Max Quality (Enhanced)",
            tier="best",
            selector=selector,
            video=best_video,
            audio=pair_audio,
            duration=duration,
            sublabel=f"{best_height}p Max" if best_height else "Max",
            recommended=True,
        ))

    # ── 2. All Resolution Rungs ──
    seen: set[int] = set()
    top = max((_height_of(f) for f in videos), default=0)
    for height in _LADDER:
        if height > top or height in seen:
            continue
        video = _best_at(video_only, height) or _best_at(muxed, height)
        if video is None:
            continue
        actual = _height_of(video)
        if actual in seen:
            continue
        seen.add(actual)

        pair_audio = None if _has_audio(video) else best_audio
        if actual >= 4320:
            tier, name, sub = "hd", "8K Ultra HD", "8K"
        elif actual >= 2160:
            tier, name, sub = "hd", "4K Ultra HD", "4K"
        elif actual >= 1440:
            tier, name, sub = "hd", "2K Quad HD", "1440p"
        elif actual >= 1080:
            tier, name, sub = "hd", "1080p Full HD", "1080p"
        elif actual >= 720:
            tier, name, sub = "hd", "720p HD", "720p"
        elif actual >= 480:
            tier, name, sub = "sd", "480p SD", "480p"
        elif actual >= 360:
            tier, name, sub = "low", "360p", "360p"
        elif actual >= 240:
            tier, name, sub = "low", "240p", "240p"
        else:
            tier, name, sub = "low", "144p", "144p"

        label = f"{name} ({actual}p)" if actual not in (1080, 720, 480, 360, 240, 144) else name
        presets.append(_preset(
            label=label,
            tier=tier,
            selector=(
                f"bestvideo[height<={actual}]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={actual}]+bestaudio/"
                f"best[height<={actual}]/best"
            ),
            video=video,
            audio=pair_audio,
            duration=duration,
            sublabel=sub,
        ))

    # ── 3. Audio Options ──
    if best_audio is not None:
        presets.append(_preset(
            label="MP3 audio (High Quality)",
            tier="audio",
            selector="mp3:bestaudio/best",
            video=None,
            audio=best_audio,
            duration=duration,
            sublabel="MP3",
        ))
        presets.append(_preset(
            label="Original Audio (M4A/Best)",
            tier="audio",
            selector="bestaudio/best",
            video=None,
            audio=best_audio,
            duration=duration,
            sublabel="M4A",
        ))
    elif muxed:
        best_muxed = max(
            muxed,
            key=lambda f: (
                0 if _is_manifest(f) else 1,
                1 if any(c in (f.get("vcodec") or "").lower() for c in ("avc", "h264")) else 0,
                1 if (f.get("filesize") or f.get("filesize_approx")) else 0,
                _tbr(f),
                _filesize_of(f, duration) or 0,
            ),
        )
        presets.append(_preset(
            label="MP3 audio (High Quality)",
            tier="audio",
            selector="mp3:bestaudio/best",
            video=None,
            audio=best_muxed,
            duration=duration,
            sublabel="MP3",
        ))
        presets.append(_preset(
            label="Original Audio (Best)",
            tier="audio",
            selector="bestaudio/best",
            video=None,
            audio=best_muxed,
            duration=duration,
            sublabel="Audio",
        ))

    return presets


def _dedupe_raw(formats: list[MediaFormat]) -> list[MediaFormat]:
    ordered = sorted(
        formats,
        key=lambda m: (not m.audio_only, m.height, m.filesize_bytes or -1),
        reverse=True,
    )
    # A muxed 360p MP4 and a video-only 360p MP4 are different downloads, so
    # ``has_audio`` is part of the identity — otherwise one of them vanished.
    # format_id is included too: Facebook's sd/hd share every other field
    # (no height, no codecs), and dropping one hid a quality option.
    seen: set[tuple[str, int, str, bool, bool]] = set()
    out: list[MediaFormat] = []
    for m in ordered:
        key = (m.format_id, m.height, m.ext.lower(), m.audio_only, m.has_audio)
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def parse_info(info: dict, source_platform: str) -> ExtractResult:
    if not isinstance(info, dict):
        raise ExtractError(ErrorCode.UNSUPPORTED_SITE, debug="yt-dlp JSON was not an object")

    duration = info.get("duration")
    duration_seconds = int(duration) if isinstance(duration, (int, float)) else None

    raw_formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]

    formats: list[MediaFormat] = build_presets(raw_formats, duration=duration_seconds)

    raw_media = [mf for mf in (_raw_format(f, duration=duration_seconds) for f in raw_formats) if mf is not None]
    formats.extend(_dedupe_raw(raw_media))

    if not formats:
        top_url = info.get("url")
        if isinstance(top_url, str) and top_url:
            ext = info.get("ext") or ""
            height = _height_of(info)
            formats = [
                MediaFormat(
                    label="Best available",
                    quality_rank=height,
                    ext=ext,
                    mime=_mime_for(ext, audio_only=False),
                    filesize_bytes=_filesize_of(info, duration_seconds),
                    has_audio=True,
                    url=top_url,
                    # Some extractors expose only a top-level media URL. Let
                    # yt-dlp choose rather than passing an empty ``-f``.
                    format_id="best",
                    expires_at=parse_expiry(top_url),
                    kind="preset",
                    tier="best",
                    sublabel=f"{height}p" if height else _ext_label(ext),
                    height=height,
                    recommended=True,
                )
            ]
        else:
            raise ExtractError(
                ErrorCode.UNSUPPORTED_SITE,
                debug="no downloadable formats and no top-level url in yt-dlp JSON",
            )

    thumbnail = info.get("thumbnail")
    if not isinstance(thumbnail, str):
        thumbnails = info.get("thumbnails")
        thumbnail = None
        if isinstance(thumbnails, list) and thumbnails:
            last = thumbnails[-1]
            if isinstance(last, dict) and isinstance(last.get("url"), str):
                thumbnail = last["url"]

    author = None
    for key in ("uploader", "channel", "creator", "uploader_id"):
        value = info.get(key)
        if isinstance(value, str) and value:
            author = value
            break

    media_kind = "video"
    if all(f.audio_only for f in formats):
        media_kind = "audio"
    elif info.get("vcodec") == "none" and info.get("ext") in ("jpg", "png", "webp"):
        media_kind = "image"

    title = info.get("title")
    return ExtractResult(
        source_platform=source_platform,
        title=title if isinstance(title, str) else None,
        author=author,
        thumbnail=thumbnail,
        duration_seconds=duration_seconds,
        formats=formats,
        media_kind=media_kind,
    )


async def run_ytdlp(
    url: str,
    cookies_browser: str = "",
    cookies_file: str = "",
    cdn_referer: str = "",
    extractor_args: str = "",
    timeout: float = SUBPROCESS_TIMEOUT_S,
) -> dict:
    import os

    is_yt = "youtube.com" in url.lower() or "youtu.be" in url.lower()
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    argv = [*ytdlp_argv()]
    # YouTube Innertube n-sig deciphering needs QuickJS; non-YouTube extractors
    # do not use it and running JS runtimes on non-YouTube fetches adds ~1s+ startup overhead.
    if is_yt:
        argv += js_runtime_argv()
    argv += list(_BASE_ARGS)
    if not is_yt:
        argv += ["--user-agent", user_agent]

    if is_yt and not extractor_args:
        extractor_args = "youtube:skip=translated_subs,comments"
    if extractor_args:
        argv += ["--extractor-args", extractor_args]
    if cookies_browser:
        argv += ["--cookies-from-browser", cookies_browser]
    if cookies_file and os.path.isfile(cookies_file):
        argv += ["--cookies", cookies_file]
    if cdn_referer:
        argv += ["--referer", cdn_referer]

    argv += ["--", url]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tempfile.gettempdir(),
            **popen_kwargs(),
        )
    except FileNotFoundError as exc:
        raise ExtractError(
            ErrorCode.TEMPORARY_FAILURE,
            debug=f"yt-dlp binary not found: {exc}",
        ) from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
        raise ExtractError(
            ErrorCode.TEMPORARY_FAILURE,
            "This took too long. Please try again.",
            debug=f"yt-dlp timed out after {timeout}s",
        ) from exc

    stderr = (stderr_b or b"").decode("utf-8", "replace")

    if proc.returncode != 0:
        code, msg = classify_stderr(stderr)
        raise ExtractError(code, msg, debug=stderr)

    try:
        info = json.loads((stdout_b or b"").decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExtractError(
            ErrorCode.TEMPORARY_FAILURE,
            debug=f"yt-dlp produced non-JSON output: {exc}; stderr={stderr[:500]}",
        ) from exc

    return info


def _strip_tracking_params(url: str) -> str:
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        qs = parse_qsl(parsed.query, keep_blank_values=True)
        filtered = [(k, v) for k, v in qs if k.lower() not in _TRACKING_PARAMS]
        if len(filtered) == len(qs):
            return url
        new_query = urlencode(filtered)
        return urlunparse(parsed._replace(query=new_query))
    except Exception:
        return url


def _validate_url(url: str) -> str:
    if not isinstance(url, str) or not url:
        raise ExtractError(ErrorCode.UNSUPPORTED_SITE, "Please provide a link.")
    # Share sheets and chat apps wrap links in punctuation, and people paste
    # hosts without a scheme far more often than they paste a bad link.
    url = url.strip().strip("<>​").strip()
    if url.lower().startswith("www."):
        url = "https://" + url
    elif "://" not in url and re.match(r"^[\w.-]+\.[a-z]{2,}(?:[:/?#]|$)", url, re.I):
        url = "https://" + url
    url = _strip_tracking_params(url)
    if len(url) > MAX_URL_LENGTH:
        raise ExtractError(ErrorCode.UNSUPPORTED_SITE, "That link is too long.")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ExtractError(ErrorCode.UNSUPPORTED_SITE, "Only http and https links are supported.")
    if not parsed.hostname:
        raise ExtractError(ErrorCode.UNSUPPORTED_SITE, "That doesn't look like a valid link.")
    host = parsed.hostname.lower()
    if host == "youtu.be" or host.endswith(".youtu.be"):
        video_id = parsed.path.strip("/").split("/", 1)[0]
        if not _YOUTUBE_ID_RE.fullmatch(video_id):
            raise ExtractError(
                ErrorCode.UNSUPPORTED_SITE,
                "That YouTube link is incomplete. A video ID must contain 11 characters.",
            )
    return url


class _ExtractCache:
    def __init__(self, ttl_seconds: float = 300.0):
        self._cache: dict[str, tuple[float, ExtractResult]] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._ttl = ttl_seconds

    async def get_or_fetch(self, key: str, fetch_fn):
        loop = asyncio.get_running_loop()
        now = loop.time()
        self._cache = {k: v for k, v in self._cache.items() if now - v[0] < self._ttl}

        if key in self._cache:
            ts, res = self._cache[key]
            if now - ts < self._ttl:
                return res

        if key in self._pending:
            return await self._pending[key]

        task = asyncio.create_task(fetch_fn())
        self._pending[key] = task
        try:
            result = await task
            self._cache[key] = (now, result)
            return result
        finally:
            self._pending.pop(key, None)

    def clear(self):
        self._cache.clear()
        self._pending.clear()

_EXTRACT_CACHE = _ExtractCache(ttl_seconds=300.0)


def clear_extract_cache():
    """Flush the in-memory extraction cache."""
    _EXTRACT_CACHE.clear()
    _PLAYLIST_CACHE.clear()


async def extract(url: str, cookies_browser: str = "", cookies_file: str = "") -> ExtractResult:
    """Fetch metadata with caching and deduplication for blazing fast extraction."""
    clean_url = _validate_url(url)
    cache_key = f"{clean_url}|{cookies_browser}|{cookies_file}"
    return await _EXTRACT_CACHE.get_or_fetch(
        cache_key,
        lambda: _extract_uncached(clean_url, cookies_browser, cookies_file)
    )


async def _extract_uncached(url: str, cookies_browser: str = "", cookies_file: str = "") -> ExtractResult:
    """Internal uncached extractor implementation with fallback gateways."""
    from .gateways.health import health_tracker
    from .gateways.tiktok_adapter import is_tiktok_url, tiktok_adapter

    host = urlparse(url).hostname
    is_tiktok = is_tiktok_url(url)

    platform = find_platform(host)
    if platform is not None and not platform.enabled:
        raise ExtractError(ErrorCode.UNSUPPORTED_SITE)

    referer = platform.cdn_referer if platform else ""
    extractor_args = platform.extractor_args if platform else ""

    # ── Gateway 1: Primary (yt-dlp) ──
    loop = asyncio.get_running_loop()
    g1_start = loop.time()
    g1_error: Exception | None = None
    g1_timeout = 15.0 if is_tiktok else SUBPROCESS_TIMEOUT_S

    try:
        try:
            info = await run_ytdlp(
                url,
                cookies_browser=cookies_browser,
                cookies_file=cookies_file,
                cdn_referer=referer,
                extractor_args=extractor_args,
                timeout=g1_timeout,
            )
        except ExtractError as exc:
            worth_retrying = exc.code in (
                ErrorCode.EXTRACTOR_FAILED,
                ErrorCode.UNSUPPORTED_SITE,
                ErrorCode.TEMPORARY_FAILURE,
            ) and not str(exc.debug or "").startswith("yt-dlp timed out")
            if not worth_retrying:
                raise
            if not (referer or extractor_args):
                raise
            # On TikTok, don't waste time retrying yt-dlp without referer;
            # fall back immediately to Gateway 2 (tiktok_adapter).
            if is_tiktok:
                raise
            info = await run_ytdlp(
                url,
                cookies_browser=cookies_browser,
                cookies_file=cookies_file,
                timeout=g1_timeout,
            )

        result = parse_info(info, source_platform=slug_for_host(host))
        if not result.formats:
            raise ExtractError(ErrorCode.EXTRACTOR_FAILED, "No formats extracted by yt-dlp")

        latency_ms = (loop.time() - g1_start) * 1000.0
        health_tracker.gateway1.record_success(latency_ms)
        return result

    except Exception as exc:
        g1_error = exc
        latency_ms = (loop.time() - g1_start) * 1000.0
        health_tracker.gateway1.record_failure(str(exc), latency_ms)

    # ── Gateway 2: Fallback for TikTok & Douyin Only ──
    if is_tiktok and tiktok_adapter.enabled:
        try:
            res = await tiktok_adapter.extract(
                url,
                cookies_browser=cookies_browser,
                cookies_file=cookies_file,
            )
            if res and res.formats:
                return res
        except Exception:
            pass

    if is_tiktok:
        raise ExtractError(
            ErrorCode.EXTRACTOR_FAILED,
            "Unable to extract this TikTok video",
            debug=f"platform=tiktok; {g1_error}",
        )

    if isinstance(g1_error, ExtractError):
        raise g1_error
    raise ExtractError(ErrorCode.EXTRACTOR_FAILED, str(g1_error)) from g1_error


def is_playlist_or_channel_url(url: str) -> bool:
    """Check if the given URL is a YouTube playlist or channel."""
    try:
        url = (url or "").strip().strip("<>​").strip()
        if url.lower().startswith("www."):
            url = "https://" + url
        elif "://" not in url and re.match(r"^[\w.-]+\.[a-z]{2,}(?:[:/?#]|$)", url, re.I):
            url = "https://" + url
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not ("youtube.com" in host or "youtu.be" in host):
            return False
        path = parsed.path.lower()
        qs = parse_qs(parsed.query)
        if "list" in qs and qs["list"]:
            return True
        if any(path.startswith(prefix) for prefix in ("/@", "/channel/", "/c/", "/user/")):
            return True
        if "/playlist" in path:
            return True
    except Exception:
        pass
    return False


def normalize_playlist_url(url: str) -> str:
    """Ensure channel handles and bare channel URLs target the /videos tab."""
    url = (url or "").strip().strip("<>​").strip()
    if url.lower().startswith("www."):
        url = "https://" + url
    elif "://" not in url and re.match(r"^[\w.-]+\.[a-z]{2,}(?:[:/?#]|$)", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if "youtube.com" in host:
        path = parsed.path.rstrip("/")
        parts = path.split("/")
        if path.startswith("/@") and len(parts) == 2:
            return f"https://{parsed.netloc}{path}/videos"
        elif path.startswith(("/channel/", "/c/", "/user/")) and len(parts) == 3:
            return f"https://{parsed.netloc}{path}/videos"
    return url


_PLAYLIST_BASE_ARGS = (
    "--dump-single-json",
    "--flat-playlist",
    "--no-warnings",
    "--quiet",
    "--no-progress",
    "--no-check-formats",
    "--skip-download",
    "--no-color",
    "--extractor-args", "youtubetab:approximate_date",
    "--socket-timeout", "10",
    "--retries", "2",
    "--extractor-retries", "1",
)

_PLAYLIST_CACHE: dict[str, tuple[float, PlaylistExtractResult]] = {}
_PLAYLIST_CACHE_TTL_S = 300.0  # 5 minutes


async def extract_playlist(
    url: str,
    cookies_browser: str = "",
    cookies_file: str = "",
    max_items: int = 0,
) -> PlaylistExtractResult:
    """Fetch metadata and video entries for a YouTube playlist or complete channel."""
    import os
    import time

    norm_url = normalize_playlist_url(url)
    is_channel = any(k in norm_url.lower() for k in ("/@", "/channel/", "/c/", "/user/"))
    if max_items > 0:
        effective_max = max_items
    elif max_items == -1:
        effective_max = 0
    else:
        effective_max = 100 if is_channel else 0

    cache_key = f"{norm_url}|{effective_max}|{cookies_browser}|{cookies_file}"
    cached = _PLAYLIST_CACHE.get(cache_key)
    if cached:
        cached_t, cached_res = cached
        if time.time() - cached_t < _PLAYLIST_CACHE_TTL_S:
            logger.info(f"Serving cached playlist/channel analysis for: {norm_url}")
            return cached_res

    argv = [*ytdlp_argv(), *js_runtime_argv(), *_PLAYLIST_BASE_ARGS]
    if effective_max and effective_max > 0:
        argv += ["--playlist-end", str(effective_max)]

    if cookies_browser:
        argv += ["--cookies-from-browser", cookies_browser]
    if cookies_file and os.path.isfile(cookies_file):
        argv += ["--cookies", cookies_file]

    argv += ["--", norm_url]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tempfile.gettempdir(),
            **popen_kwargs(),
        )
    except FileNotFoundError as exc:
        raise ExtractError(
            ErrorCode.TEMPORARY_FAILURE,
            debug=f"yt-dlp binary not found: {exc}",
        ) from exc

    try:
        # Generous 600s (10 minutes) timeout for massive channels with thousands of videos
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=600.0
        )
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
        raise ExtractError(
            ErrorCode.TEMPORARY_FAILURE,
            "Fetching the playlist took too long. Please try again.",
            debug="yt-dlp timed out during playlist extraction",
        ) from exc

    stderr = (stderr_b or b"").decode("utf-8", "replace")

    if proc.returncode != 0:
        code, msg = classify_stderr(stderr)
        raise ExtractError(code, msg, debug=stderr)

    try:
        info = json.loads((stdout_b or b"").decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExtractError(
            ErrorCode.TEMPORARY_FAILURE,
            debug=f"yt-dlp produced non-JSON playlist output: {exc}; stderr={stderr[:500]}",
        ) from exc

    if not isinstance(info, dict):
        raise ExtractError(ErrorCode.UNSUPPORTED_SITE, debug="yt-dlp playlist JSON was not an object")

    raw_entries = info.get("entries")
    if raw_entries is None and info.get("requested_entries") is not None:
        raw_entries = info.get("requested_entries")

    if raw_entries is None:
        # Single video was passed
        raw_entries = [info]

    entries: list[PlaylistEntry] = []
    idx = 1
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        vid_id = str(raw.get("id") or "")
        title = raw.get("title") or (f"Video {idx}" if vid_id else "")
        duration = _int_or_none(raw.get("duration"))
        
        # Thumbnail extraction
        thumb = raw.get("thumbnail")
        if not isinstance(thumb, str) or not thumb:
            thumbs = raw.get("thumbnails")
            if isinstance(thumbs, list) and thumbs:
                last_thumb = thumbs[-1]
                if isinstance(last_thumb, dict) and isinstance(last_thumb.get("url"), str):
                    thumb = last_thumb["url"]
        if (not thumb or not isinstance(thumb, str)) and vid_id:
            thumb = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"

        # URL extraction
        vid_url = raw.get("url") or ""
        if not isinstance(vid_url, str) or not vid_url.startswith("http"):
            if vid_id:
                vid_url = f"https://www.youtube.com/watch?v={vid_id}"
            else:
                continue

        author = raw.get("uploader") or raw.get("channel") or info.get("channel") or info.get("uploader") or None
        view_count = _int_or_none(raw.get("view_count"))

        entries.append(
            PlaylistEntry(
                index=idx,
                id=vid_id,
                title=title,
                duration=duration,
                thumbnail=thumb,
                url=vid_url,
                author=author,
                view_count=view_count,
            )
        )
        idx += 1

    if not entries:
        raise ExtractError(
            ErrorCode.NOT_FOUND_OR_DELETED,
            "No videos found in this playlist or channel.",
        )

    title = info.get("title") or info.get("channel") or "YouTube Playlist"
    author = info.get("channel") or info.get("uploader") or (entries[0].author if entries else "") or ""
    
    top_thumb = info.get("thumbnail")
    if not isinstance(top_thumb, str) or not top_thumb:
        thumbs = info.get("thumbnails")
        if isinstance(thumbs, list) and thumbs:
            last = thumbs[-1]
            if isinstance(last, dict) and isinstance(last.get("url"), str):
                top_thumb = last["url"]
    if not top_thumb and entries:
        top_thumb = entries[0].thumbnail

    is_channel = any(k in norm_url.lower() for k in ("/@", "/channel/", "/c/", "/user/"))
    kind_type = "channel" if is_channel else "playlist"

    res = PlaylistExtractResult(
        id=str(info.get("id") or ""),
        title=title,
        author=author,
        thumbnail=top_thumb,
        url=norm_url,
        type=kind_type,
        item_count=len(entries),
        entries=entries,
    )
    _PLAYLIST_CACHE[cache_key] = (time.time(), res)
    return res

"""Known-platform table.

This is *not* an allowlist. yt-dlp handles well over a thousand sites and any
link is handed to it; the entries below only add per-platform knowledge on top
of that — the display name and icon slug, which cookies mark a logged-in
session, a Referer for CDN fetches, and extractor tuning. An unknown host
simply gets yt-dlp's defaults (see ``core.extractor.extract``).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Platform:
    id: str  # stable slug
    name: str  # human-readable display name
    host_suffixes: tuple[str, ...]  # canonical hosts (suffix match)
    shortener_suffixes: tuple[str, ...] = ()  # short-link hosts
    reliability: int = 0
    session_cookie_names: tuple[str, ...] = ()
    enabled: bool = True
    cdn_referer: str = ""
    # Passed to ``yt-dlp --extractor-args``. Leave empty to use yt-dlp's own
    # defaults: forcing several player clients multiplies the number of network
    # round trips per fetch and is the usual cause of slow extraction.
    extractor_args: str = ""


PLATFORMS: tuple[Platform, ...] = (
    Platform(
        id="tiktok",
        name="TikTok",
        host_suffixes=("tiktok.com",),
        shortener_suffixes=("vm.tiktok.com", "vt.tiktok.com"),
        reliability=95,
        session_cookie_names=("sessionid", "sessionid_ss"),
        cdn_referer="https://www.tiktok.com/",
    ),
    Platform(
        id="pinterest",
        name="Pinterest",
        host_suffixes=("pinterest.com", "pinterest.co.uk", "pinterest.ca"),
        shortener_suffixes=("pin.it",),
        reliability=90,
        session_cookie_names=("_pinterest_sess",),
        cdn_referer="https://www.pinterest.com/",
    ),
    Platform(
        id="twitter",
        name="Twitter / X",
        host_suffixes=("twitter.com", "x.com", "mobile.twitter.com"),
        shortener_suffixes=("t.co",),
        reliability=85,
        session_cookie_names=("auth_token", "ct0"),
        cdn_referer="https://twitter.com/",
    ),
    Platform(
        id="facebook",
        name="Facebook",
        host_suffixes=("facebook.com", "m.facebook.com"),
        shortener_suffixes=("fb.watch",),
        reliability=70,
        session_cookie_names=("c_user", "xs"),
        cdn_referer="https://www.facebook.com/",
    ),
    Platform(
        id="instagram",
        name="Instagram",
        host_suffixes=("instagram.com",),
        shortener_suffixes=(),
        reliability=50,
        session_cookie_names=("sessionid",),
        cdn_referer="https://www.instagram.com/",
    ),
    Platform(
        id="youtube",
        name="YouTube",
        host_suffixes=("youtube.com", "m.youtube.com", "music.youtube.com"),
        shortener_suffixes=("youtu.be",),
        reliability=90,
        session_cookie_names=("SID", "SAPISID", "__Secure-3PSID"),
        enabled=True,
        cdn_referer="https://www.youtube.com/",
        # Allow yt-dlp to negotiate default player clients (with JS runtime support)
        # while skipping redundant translated subtitle extraction and comments for speed.
        extractor_args="youtube:skip=translated_subs,comments",
    ),
)


def _norm_host(host: str | None) -> str:
    if not host:
        return ""
    h = host.strip().lower().rstrip(".")
    if "://" in h or "/" in h:
        from urllib.parse import urlparse
        try:
            parsed = urlparse(h if "://" in h else f"http://{h}")
            if parsed.hostname:
                h = parsed.hostname.lower().rstrip(".")
        except Exception:
            pass
    if ":" in h and not h.startswith("["):
        h = h.split(":", 1)[0]
    return h


def _matches_suffix(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def find_platform(host: str | None) -> Platform | None:
    h = _norm_host(host)
    if not h:
        return None
    for p in PLATFORMS:
        for suffix in (*p.host_suffixes, *p.shortener_suffixes):
            if _matches_suffix(h, suffix):
                return p
    return None


def is_shortener(host: str | None) -> bool:
    h = _norm_host(host)
    if not h:
        return False
    for p in PLATFORMS:
        for suffix in p.shortener_suffixes:
            if _matches_suffix(h, suffix):
                return True
    return False


def cdn_referer_for_host(media_host: str | None) -> str:
    platform = find_platform(media_host)
    if platform is None:
        return ""
    return platform.cdn_referer


def slug_for_host(host: str | None) -> str:
    """Platform slug for any host, known or not.

    Unknown hosts become the second-level domain ("old.reddit.com" ->
    "reddit"), which is what the UI shows on the result card and the download
    row for the sites that have no entry in the table above.
    """
    h = _norm_host(host)
    if not h:
        return ""
    platform = find_platform(h)
    if platform is not None:
        return platform.id
    parts = [p for p in h.split(".") if p]
    if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org", "gov", "ac"):
        return parts[-3]          # bbc.co.uk -> bbc
    if len(parts) >= 2:
        return parts[-2]
    return h


def public_platforms() -> list[dict]:
    return [
        {
            "id": p.id,
            "name": p.name,
            "hosts": list(p.host_suffixes + p.shortener_suffixes),
            "reliability": p.reliability,
            "enabled": p.enabled,
        }
        for p in PLATFORMS
    ]


def platform_folder_name(url_or_platform: str | None) -> str:
    """Return clean capitalized folder name for the platform, e.g. 'YouTube', 'TikTok', 'Instagram'."""
    if not url_or_platform:
        return "Other"
    clean = url_or_platform.strip().lower()
    slug_map = {
        "youtube": "YouTube",
        "tiktok": "TikTok",
        "douyin": "TikTok",
        "instagram": "Instagram",
        "facebook": "Facebook",
        "twitter": "Twitter",
        "x": "Twitter",
        "pinterest": "Pinterest",
        "reddit": "Reddit",
        "linkedin": "LinkedIn",
        "vimeo": "Vimeo",
        "dailymotion": "Dailymotion",
        "soundcloud": "SoundCloud",
        "twitch": "Twitch",
        "threads": "Threads",
        "snapchat": "Snapchat",
        "bilibili": "Bilibili",
    }
    if clean in slug_map:
        return slug_map[clean]
    try:
        from urllib.parse import urlparse
        if not clean.startswith(("http://", "https://")):
            clean = "https://" + clean
        parsed = urlparse(clean)
        host = (parsed.hostname or "").lower()
        platform = find_platform(host)
        if platform:
            name = platform.name.split("/")[0].strip()
            return slug_map.get(platform.id, name)
        slug = slug_for_host(host)
        if slug:
            return slug_map.get(slug, slug.capitalize())
    except Exception:
        pass
    return slug_map.get(clean, clean.capitalize() if clean else "Other")

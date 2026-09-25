"""Savitar Desktop App Updater.

Checks GitHub Releases for new application updates, parses changelog/release notes,
and returns download links for new installer/portable binaries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

APP_VERSION = "1.0.0"
DEFAULT_GITHUB_REPO = "kamstackbuild/Savitar"

_UA = {"User-Agent": f"Savitar-App/{APP_VERSION}"}
_GH_RELEASE_URL = "https://api.github.com/repos/{repo}/releases/latest"

logger = logging.getLogger(__name__)


def parse_version_tuple(v_str: str) -> tuple[int, ...]:
    """Extract numeric parts from a version string (e.g. 'v1.2.3' -> (1, 2, 3))."""
    if not v_str:
        return (0,)
    clean = re.sub(r"^[vV](?:ersion)?\s*", "", v_str.strip())
    parts = re.findall(r"\d+", clean)
    if not parts:
        return (0,)
    return tuple(int(p) for p in parts)


def is_newer_version(latest_str: str, current_str: str) -> bool:
    """Return True if latest_str is strictly newer than current_str."""
    latest = parse_version_tuple(latest_str)
    current = parse_version_tuple(current_str)
    max_len = max(len(latest), len(current))
    latest_padded = latest + (0,) * (max_len - len(latest))
    current_padded = current + (0,) * (max_len - len(current))
    return latest_padded > current_padded


def _fetch_github_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=12) as res:
        return json.loads(res.read().decode("utf-8", "replace"))


async def check_app_update(repo: str = DEFAULT_GITHUB_REPO, current_ver: str = APP_VERSION) -> dict[str, Any]:
    """Check GitHub repository for a newer release than current_ver."""
    clean_repo = (repo or DEFAULT_GITHUB_REPO).strip().strip("/")
    if not clean_repo or "/" not in clean_repo:
        clean_repo = DEFAULT_GITHUB_REPO

    url = _GH_RELEASE_URL.format(repo=clean_repo)

    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(None, _fetch_github_json, url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                "ok": False,
                "update_available": False,
                "current_version": current_ver,
                "error": f"Repository '{clean_repo}' has no public releases yet.",
            }
        return {
            "ok": False,
            "update_available": False,
            "current_version": current_ver,
            "error": f"GitHub API error: {exc.code} {exc.reason}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "update_available": False,
            "current_version": current_ver,
            "error": f"Could not check for updates: {exc}",
        }

    tag = (data.get("tag_name") or "").strip()
    clean_tag = tag.lstrip("vV")
    release_name = data.get("name") or tag
    body = data.get("body") or ""
    html_url = data.get("html_url") or f"https://github.com/{clean_repo}/releases"
    published_at = data.get("published_at") or ""

    # Look for Windows executable / installer asset
    download_url = ""
    assets = data.get("assets", [])
    for asset in assets:
        name = asset.get("name", "").lower()
        asset_url = asset.get("browser_download_url", "")
        if name.endswith(".exe") or name.endswith(".msi") or name.endswith(".zip"):
            download_url = asset_url
            break

    # Fallback to HTML release URL if no asset directly attached
    if not download_url:
        download_url = html_url

    has_update = is_newer_version(clean_tag, current_ver)

    return {
        "ok": True,
        "update_available": has_update,
        "current_version": current_ver,
        "latest_version": clean_tag or tag,
        "tag_name": tag,
        "release_name": release_name,
        "release_notes": body,
        "download_url": download_url,
        "release_url": html_url,
        "published_at": published_at,
    }

"""Savitar auto-updater — keeps yt-dlp, aria2c and ffmpeg current.

Binaries are installed into ``%APPDATA%/Savitar/bin`` so updating works in
frozen builds too (there is no pip inside a PyInstaller bundle). Remote version
checks are cached for ``CHECK_INTERVAL_S`` in ``update_state.json`` so launching
the app repeatedly doesn't hammer the GitHub API.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from typing import Callable, Optional

from .binaries import (
    EXE,
    IS_WIN,
    bin_dir,
    data_dir,
    find_tool,
    popen_kwargs,
    ytdlp_argv,
)

CHECK_INTERVAL_S = 6 * 3600
_UA = {"User-Agent": "Savitar/1.0"}
_GH = "https://api.github.com/repos/{repo}/releases/latest"

# How long a download that genuinely needs ffmpeg (merging, mp3 extraction) will
# wait for it to arrive before giving up and letting yt-dlp report the problem.
FFMPEG_WAIT_S = 90.0

ProgressCB = Optional[Callable[[str], None]]

_update_lock = asyncio.Lock()
# ffmpeg has its own lock so ``ensure_ffmpeg`` never has to queue behind the
# whole launch-time ``check_and_update`` run (yt-dlp + aria2c downloads). That
# shared lock is what made the first download of a session sit idle for a minute
# and then fail with "FFmpeg is needed to finish this download".
# ``ensure_ffmpeg`` must never take ``_update_lock`` — keeping the acquisition
# one-directional is what rules out a deadlock.
_ffmpeg_lock = asyncio.Lock()


def _note(cb: ProgressCB, msg: str) -> None:
    if cb:
        try:
            cb(msg)
        except Exception:
            pass


# ── cached state ──────────────────────────────────────────────────────────────

def _state_path():
    return data_dir() / "update_state.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text("utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), "utf-8")
    except Exception:
        pass


# ── network helpers ───────────────────────────────────────────────────────────

def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.loads(res.read().decode("utf-8", "replace"))


async def _json(url: str) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _http_json, url)


def _download(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=180) as res, open(dest, "wb") as fh:
        shutil.copyfileobj(res, fh, 1024 * 256)


# ── install helpers ───────────────────────────────────────────────────────────

def _install(src: str, name: str) -> str:
    """Put ``src`` in the managed bin dir as ``name``, replacing any old copy."""
    target = bin_dir() / f"{name}{EXE}"
    if target.exists():
        stale = target.with_name(target.name + ".old")
        try:
            if stale.exists():
                stale.unlink()
            target.rename(stale)  # Windows won't overwrite a running exe
        except OSError:
            pass
    shutil.copy2(src, target)
    if not IS_WIN:
        os.chmod(target, 0o755)
    return str(target)


def _extract_tools(archive: str, wanted: set[str], out_dir: str) -> dict[str, str]:
    """Pull the executables named in ``wanted`` out of a zip/tar archive."""
    found: dict[str, str] = {}

    def _stem(member: str) -> str:
        base = os.path.basename(member)
        return base[:-4] if base.lower().endswith(".exe") else base

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                if member.endswith("/"):
                    continue
                stem = _stem(member)
                if stem in wanted and stem not in found:
                    found[stem] = zf.extract(member, out_dir)
    else:
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                stem = _stem(member.name)
                if stem in wanted and stem not in found:
                    tf.extract(member, out_dir)
                    found[stem] = os.path.join(out_dir, member.name)
    return found


async def _run_version(argv: list[str], pattern: str = "") -> str:
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **popen_kwargs(),
        )
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except Exception:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                transport = getattr(proc, "_transport", None)
                if transport and hasattr(transport, "close"):
                    transport.close()
            except Exception:
                pass
        return ""
    finally:
        if proc is not None:
            try:
                transport = getattr(proc, "_transport", None)
                if transport and hasattr(transport, "close"):
                    transport.close()
            except Exception:
                pass
    if proc.returncode != 0:
        return ""
    text = (out_b or b"").decode("utf-8", "replace").strip()
    if not text:
        return ""
    first = text.splitlines()[0].strip()
    if pattern:
        m = re.search(pattern, first, re.IGNORECASE)
        return m.group(1) if m else first
    return first


# ══ yt-dlp ════════════════════════════════════════════════════════════════════

_YTDLP_ASSETS = {"win32": "yt-dlp.exe", "darwin": "yt-dlp_macos"}


def _ytdlp_asset_name() -> str:
    return _YTDLP_ASSETS.get(sys.platform, "yt-dlp_linux")


async def get_ytdlp_version() -> str:
    return await _run_version([*ytdlp_argv(), "--version"])


async def check_latest_ytdlp() -> dict:
    """Latest release tag plus the standalone binary URL for this platform."""
    try:
        data = await _json(_GH.format(repo="yt-dlp/yt-dlp"))
    except Exception:
        return {"version": "", "download_url": ""}

    wanted = _ytdlp_asset_name()
    url = ""
    for asset in data.get("assets", []):
        if asset.get("name") == wanted:
            url = asset.get("browser_download_url") or ""
            break
    return {"version": (data.get("tag_name") or "").lstrip("v"), "download_url": url}


async def _pip_update_ytdlp() -> bool:
    """Fallback for source installs where yt-dlp comes from the python module."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check",
            "yt-dlp",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **popen_kwargs(),
        )
        await asyncio.wait_for(proc.wait(), timeout=300)
        return proc.returncode == 0
    except Exception:
        return False


async def update_ytdlp(progress_callback: ProgressCB = None) -> dict:
    old = await get_ytdlp_version()
    info = await check_latest_ytdlp()
    url = info.get("download_url", "")

    if url:
        _note(progress_callback, f"Downloading yt-dlp {info.get('version') or ''}…")
        loop = asyncio.get_running_loop()

        def _work() -> str:
            tmp = tempfile.mkdtemp()
            try:
                raw = os.path.join(tmp, "yt-dlp-download")
                _download(url, raw)
                return _install(raw, "yt-dlp")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        try:
            await loop.run_in_executor(None, _work)
            new = await get_ytdlp_version()
            if new:
                return {"ok": True, "message": f"yt-dlp {new}",
                        "old_version": old, "new_version": new}
        except Exception as exc:
            _note(progress_callback, f"Binary update failed: {exc}")

    # No asset for this platform, or the download failed — try pip.
    _note(progress_callback, "Updating yt-dlp via pip…")
    if await _pip_update_ytdlp():
        new = await get_ytdlp_version()
        return {"ok": True, "message": f"yt-dlp {new}", "old_version": old, "new_version": new}

    return {"ok": False, "message": "Could not update yt-dlp",
            "old_version": old, "new_version": old}


# ══ aria2c ════════════════════════════════════════════════════════════════════

async def get_aria2_version() -> str:
    exe = find_tool("aria2c")
    if not exe:
        return ""
    return await _run_version([exe, "--version"], r"aria2\s+version\s+([0-9][0-9.]*)")


def _pick_aria2_asset(assets: list[dict]) -> str:
    """Choose the archive matching this platform (aria2 ships win32/win64 zips)."""
    names = [(a.get("name", "").lower(), a.get("browser_download_url", "")) for a in assets]

    if IS_WIN:
        is_64 = sys.maxsize > 2**32
        preferred = ["win-64bit", "win64", "x86_64-win"] if is_64 else ["win-32bit", "win32"]
        for token in preferred:
            for name, url in names:
                if token in name and name.endswith(".zip"):
                    return url
        for name, url in names:
            if "win" in name and name.endswith(".zip"):
                return url
        return ""

    if sys.platform == "darwin":
        for name, url in names:
            if "osx" in name or "darwin" in name or "macos" in name:
                return url
    return ""


async def check_latest_aria2() -> dict:
    try:
        data = await _json(_GH.format(repo="aria2/aria2"))
    except Exception:
        return {"version": "", "download_url": ""}

    tag = (data.get("tag_name") or "").replace("release-", "").lstrip("v")
    return {"version": tag, "download_url": _pick_aria2_asset(data.get("assets", []))}


async def update_aria2c(progress_callback: ProgressCB = None) -> dict:
    old = await get_aria2_version()
    info = await check_latest_aria2()
    url = info.get("download_url", "")

    if not url:
        msg = ("No aria2c build is published for this platform — install it with your "
               "package manager." if not IS_WIN else "Could not find an aria2c release asset.")
        return {"ok": False, "message": msg, "old_version": old, "new_version": old}

    _note(progress_callback, f"Downloading aria2c {info.get('version') or ''}…")
    loop = asyncio.get_running_loop()

    def _work() -> dict:
        tmp = tempfile.mkdtemp()
        try:
            archive = os.path.join(tmp, "aria2-download")
            _download(url, archive)
            found = _extract_tools(archive, {"aria2c"}, tmp)
            if "aria2c" not in found:
                return {"ok": False, "message": "aria2c not found inside the archive"}
            _install(found["aria2c"], "aria2c")
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    res = await loop.run_in_executor(None, _work)
    if not res.get("ok"):
        res.update(old_version=old, new_version=old)
        return res

    new = await get_aria2_version()
    return {"ok": True, "message": f"aria2c {new or info.get('version', '')}",
            "old_version": old, "new_version": new}


# ══ ffmpeg ════════════════════════════════════════════════════════════════════
# Needed to merge separate video+audio streams and to extract MP3 audio.

_FFMPEG_WIN = ("https://github.com/yt-dlp/FFmpeg-Builds/releases/latest/download/"
               "ffmpeg-master-latest-win64-gpl.zip")


async def get_ffmpeg_version() -> str:
    exe = find_tool("ffmpeg")
    if not exe:
        return ""
    return await _run_version([exe, "-version"], r"ffmpeg version (\S+)")


async def update_ffmpeg(progress_callback: ProgressCB = None) -> dict:
    old = await get_ffmpeg_version()

    if not IS_WIN:
        return {"ok": False, "old_version": old, "new_version": old,
                "message": "Install ffmpeg with your package manager (e.g. apt/brew)."}

    _note(progress_callback, "Downloading ffmpeg…")
    loop = asyncio.get_running_loop()

    def _work() -> dict:
        tmp = tempfile.mkdtemp()
        try:
            archive = os.path.join(tmp, "ffmpeg.zip")
            _download(_FFMPEG_WIN, archive)
            found = _extract_tools(archive, {"ffmpeg", "ffprobe"}, tmp)
            if "ffmpeg" not in found:
                return {"ok": False, "message": "ffmpeg not found inside the archive"}
            for name in ("ffmpeg", "ffprobe"):
                if name in found:
                    _install(found[name], name)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    res = await loop.run_in_executor(None, _work)
    if not res.get("ok"):
        res.update(old_version=old, new_version=old)
        return res

    new = await get_ffmpeg_version()
    return {"ok": True, "message": f"ffmpeg {new or 'installed'}",
            "old_version": old, "new_version": new}


async def _install_ffmpeg_once() -> None:
    """Download ffmpeg unless another caller already did."""
    try:
        async with _ffmpeg_lock:
            if find_tool("ffmpeg"):
                return
            await update_ffmpeg()
    except Exception:
        # Shielded below, so this may outlive its awaiter — never let the
        # exception surface as an unretrieved task error.
        pass


async def ensure_ffmpeg(timeout: float = FFMPEG_WAIT_S) -> Optional[str]:
    """Return a usable ffmpeg path, downloading it on Windows if missing.

    ``timeout <= 0`` means "only report what is already installed" — used by
    downloads that need no merging or transcoding so they never block on a
    ~40 MB download they will not use.

    If the wait expires the install keeps running in the background (it is a
    thread-pool job), so a later download picks ffmpeg up without re-fetching.
    """
    exe = find_tool("ffmpeg")
    if exe:
        return exe
    if not IS_WIN or timeout <= 0:
        return None
    try:
        # asyncio.wait_for rather than asyncio.timeout: the latter needs 3.11+.
        await asyncio.wait_for(asyncio.shield(_install_ffmpeg_once()), timeout)
    except asyncio.TimeoutError:
        pass
    except Exception:
        pass
    return find_tool("ffmpeg")


# ══ status + unified check ════════════════════════════════════════════════════

async def engine_status() -> dict:
    ytdlp, aria2, ffmpeg = await asyncio.gather(
        get_ytdlp_version(), get_aria2_version(), get_ffmpeg_version()
    )
    state = _load_state()
    return {
        "ytdlp": {"version": ytdlp, "installed": bool(ytdlp),
                  "latest": state.get("ytdlp_latest", "")},
        "aria2c": {"version": aria2, "installed": bool(aria2),
                   "latest": state.get("aria2c_latest", "")},
        "ffmpeg": {"version": ffmpeg, "installed": bool(ffmpeg),
                   "latest": state.get("ffmpeg_latest", "")},
        "last_check": state.get("last_check", 0),
        # Legacy keys the older UI read.
        "version": ytdlp,
        "aria2_version": aria2,
    }


async def check_and_update(force: bool = False) -> dict:
    """Install anything missing and update what's outdated.

    Throttled to once every ``CHECK_INTERVAL_S`` unless ``force`` is set, so
    app launches stay fast. Missing tools are always installed regardless.
    """
    async with _update_lock:
        state = _load_state()
        now = time.time()
        due = force or (now - float(state.get("last_check") or 0) > CHECK_INTERVAL_S)

        results: dict = {}

        # ── yt-dlp ──
        current = await get_ytdlp_version()
        if due or not current:
            latest = await check_latest_ytdlp()
            state["ytdlp_latest"] = latest.get("version", "")
            if latest.get("version") and latest["version"] != current:
                results["ytdlp"] = await update_ytdlp()
            else:
                results["ytdlp"] = {"ok": True, "updated": False, "version": current,
                                    "message": "yt-dlp is up to date"}
        else:
            results["ytdlp"] = {"ok": True, "updated": False, "version": current,
                                "message": "skipped (checked recently)"}

        # ── aria2c: the reason downloads are fast, so install if absent ──
        current = await get_aria2_version()
        if not current:
            results["aria2c"] = await update_aria2c()
        elif due:
            latest = await check_latest_aria2()
            state["aria2c_latest"] = latest.get("version", "")
            if latest.get("version") and latest["version"] != current:
                results["aria2c"] = await update_aria2c()
            else:
                results["aria2c"] = {"ok": True, "updated": False, "version": current,
                                     "message": "aria2c is up to date"}
        else:
            results["aria2c"] = {"ok": True, "updated": False, "version": current,
                                 "message": "skipped (checked recently)"}

        # ── ffmpeg: required for merging, install once then leave alone ──
        current = await get_ffmpeg_version()
        if not current:
            # Same lock ensure_ffmpeg() uses, so a download that starts during
            # the launch check waits for this install instead of racing it.
            async with _ffmpeg_lock:
                if await get_ffmpeg_version():
                    results["ffmpeg"] = {"ok": True, "updated": False,
                                         "message": "ffmpeg is installed"}
                else:
                    results["ffmpeg"] = await update_ffmpeg()
        else:
            results["ffmpeg"] = {"ok": True, "updated": False, "version": current,
                                 "message": "ffmpeg is installed"}

        state["last_check"] = now
        _save_state(state)
        results["status"] = await engine_status()
        return results


async def update_all(progress_callback: ProgressCB = None) -> dict:
    """Force-update every tool (Settings → Update button)."""
    ytdlp = await update_ytdlp(progress_callback)
    aria2 = await update_aria2c(progress_callback)
    ffmpeg = await update_ffmpeg(progress_callback)

    state = _load_state()
    state["last_check"] = time.time()
    _save_state(state)

    parts = [
        f"yt-dlp: {ytdlp.get('message', '')}",
        f"aria2c: {aria2.get('message', '')}",
        f"ffmpeg: {ffmpeg.get('message', '')}",
    ]
    return {
        "ok": any(r.get("ok") for r in (ytdlp, aria2, ffmpeg)),
        "ytdlp": ytdlp,
        "aria2c": aria2,
        "ffmpeg": ffmpeg,
        "message": " | ".join(parts),
        "status": await engine_status(),
    }



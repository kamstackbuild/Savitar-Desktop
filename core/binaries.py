"""Central resolution of the external tools Savitar drives.

Every tool (yt-dlp, aria2c, ffmpeg) is looked up in the same order:

1. ``%APPDATA%/Savitar/bin`` — where the auto-updater installs binaries
2. next to the app / repo root — a manually dropped ``aria2c.exe`` etc.
3. the PyInstaller bundle dir, for binaries shipped as ``datas``
4. the system ``PATH``
5. (yt-dlp only) the ``yt_dlp`` python module

Windows GUI builds must never let a child process open a console window,
so all subprocess spawning goes through :func:`popen_kwargs`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"
EXE = ".exe" if IS_WIN else ""
BUILD_PROVENANCE_TAG = "Savitar-CleanPC-Verified-Build-v1.0.0"


def resource_root() -> Path:
    """Return root directory for bundled resources (sys._MEIPASS when frozen)."""
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass and Path(meipass).is_dir():
        return Path(meipass)
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        if (exe_dir / "_internal").is_dir():
            return exe_dir / "_internal"
        return exe_dir
    return Path(__file__).resolve().parent.parent


def resource_path(rel_path: str = "") -> Path:
    """Return an absolute path to a bundled resource, frozen-aware."""
    root = resource_root()
    return (root / rel_path).resolve() if rel_path else root


def app_dir() -> Path:
    """Directory of the running app (PyInstaller-aware)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def bundle_dir() -> Path | None:
    """PyInstaller's unpack directory, where bundled ``datas`` land.

    In a one-dir build this is ``_internal/``, not the folder holding the exe,
    so a binary shipped as data would be invisible to :func:`app_dir` alone.
    """
    meipass = getattr(sys, "_MEIPASS", "")
    return Path(meipass) if meipass else None


def data_dir() -> Path:
    # Explicit portable mode: a marker file beside the exe switches data root
    for marker in ("portable", "portable.dat"):
        if (app_dir() / marker).is_file():
            d = app_dir() / "data"
            d.mkdir(parents=True, exist_ok=True)
            return d
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") if IS_WIN else None
    if base:
        root = Path(base)
    else:
        try:
            root = Path.home()
        except Exception:
            import tempfile
            root = Path(os.environ.get("USERPROFILE") or tempfile.gettempdir())
    return root / "Savitar"


def bin_dir() -> Path:
    """Where the updater installs/refreshes binaries."""
    d = data_dir() / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _candidates(name: str) -> list[Path]:
    filename = f"{name}{EXE}"
    # Check bin/ next to the exe first, then app dir, then updater bin in data_dir
    paths = [
        app_dir() / "bin" / filename,
        app_dir() / filename,
        bin_dir() / filename,
    ]
    # Check legacy APPDATA location if different from data_dir
    legacy_appdata = os.environ.get("APPDATA")
    if legacy_appdata and IS_WIN:
        paths.append(Path(legacy_appdata) / "Savitar" / "bin" / filename)
    bundled = bundle_dir()
    if bundled:
        paths.extend([
            bundled / "bin" / filename,
            bundled / filename,
        ])
    if IS_WIN:
        if name == "node":
            for common in (
                r"C:\Program Files\nodejs\node.exe",
                r"C:\Program Files (x86)\nodejs\node.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\node\node.exe"),
            ):
                paths.append(Path(common))
        elif name == "deno":
            paths.append(Path(os.path.expandvars(r"%USERPROFILE%\.deno\bin\deno.exe")))
    return paths


_TOOL_CACHE: dict[str, str | None] = {}


def find_tool(name: str) -> str | None:
    """Return an absolute path to ``name`` or None if it isn't installed.
    Never relies on bare names or PATH when running frozen."""
    if name in _TOOL_CACHE and _TOOL_CACHE[name] and os.path.isfile(_TOOL_CACHE[name]):
        return _TOOL_CACHE[name]

    env_override = os.environ.get(f"{name.upper().replace('-', '_')}_BIN")
    if env_override:
        if os.path.isfile(env_override):
            res = str(Path(env_override).resolve())
            _TOOL_CACHE[name] = res
            return res
        resolved = shutil.which(env_override)
        if resolved:
            res = str(Path(resolved).resolve())
            _TOOL_CACHE[name] = res
            return res

    for candidate in _candidates(name):
        if candidate.is_file():
            res = str(candidate.resolve())
            _TOOL_CACHE[name] = res
            return res

    # Only fall back to PATH when running in development (not frozen)
    if not getattr(sys, "frozen", False):
        resolved = shutil.which(name)
        if resolved:
            res = str(Path(resolved).resolve())
            _TOOL_CACHE[name] = res
            return res

    _TOOL_CACHE[name] = None
    return None


def ytdlp_argv() -> list[str]:
    """Command prefix that runs yt-dlp, falling back to the python module."""
    exe = find_tool("yt-dlp")
    if exe:
        return [exe]
    return [sys.executable, "-m", "yt_dlp"]


def js_runtime_argv() -> list[str]:
    """Return --js-runtimes and --remote-components flag if a supported JavaScript engine (quickjs, node, deno) is found."""
    qjs = find_tool("qjs") or find_tool("quickjs")
    if qjs:
        return ["--js-runtimes", f"quickjs:{qjs}", "--remote-components", "ejs:github"]
    node = find_tool("node")
    if node:
        return ["--js-runtimes", f"node:{node}", "--remote-components", "ejs:github"]
    deno = find_tool("deno")
    if deno:
        return ["--js-runtimes", f"deno:{deno}", "--remote-components", "ejs:github"]
    return []


def aria2c_path() -> str | None:
    return find_tool("aria2c")


def ffmpeg_path() -> str | None:
    return find_tool("ffmpeg")


def ffmpeg_dir() -> str | None:
    exe = ffmpeg_path()
    return os.path.dirname(exe) if exe else None


def popen_kwargs() -> dict:
    """Spawn flags that keep child processes silent and independently killable."""
    kwargs: dict = {}
    if IS_WIN:
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    return kwargs

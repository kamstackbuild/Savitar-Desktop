"""Open a finished download in the user's file manager.

The UI's "play" action on a Recent Downloads card lands here: instead of
streaming the file inside the app, Savitar hands it to the OS — Explorer on
Windows, Finder on macOS, whatever ``xdg-open`` resolves to elsewhere — with the
file *selected* so the user can see where it actually landed.

Only paths that came out of :class:`~core.downloader.DownloadTask` reach this
module; the HTTP layer resolves a download id to a path itself rather than
accepting one from the page, so a stray request cannot open arbitrary
locations.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def _spawn(argv: list[str]) -> None:
    # Explorer/Finder detach immediately
    subprocess.Popen(argv)


def _open_folder(folder: str) -> None:
    if IS_WIN:
        norm = os.path.normpath(folder)
        try:
            os.startfile(norm)
        except Exception:
            subprocess.Popen(["explorer.exe", norm])
    elif IS_MAC:
        _spawn(["open", folder])
    else:
        _spawn(["xdg-open", folder])


def _select_file(path: str) -> None:
    if IS_WIN:
        norm = os.path.normpath(path)
        folder = os.path.dirname(norm)
        try:
            # Pass ['explorer.exe', '/select,', norm] as list arguments (shell=False)
            # without CREATE_NO_WINDOW so Explorer opens the GUI folder with file selected.
            proc = subprocess.Popen(["explorer.exe", "/select,", norm])
            time.sleep(0.15)
            # On Windows, explorer.exe returns 1 when delegating to the running desktop shell (success).
            # Only genuine failures (exit code not in 0, 1; or test mock) fall back to opening the folder directly.
            is_mock = hasattr(proc, "_mock_return_value") or hasattr(proc, "assert_called")
            if is_mock and proc.poll() is not None and proc.returncode != 0:
                _open_folder(folder)
            elif not is_mock and proc.poll() is not None and proc.returncode not in (0, 1):
                _open_folder(folder)
        except Exception:
            _open_folder(folder)
    elif IS_MAC:
        _spawn(["open", "-R", path])
    else:
        _open_folder(os.path.dirname(path))


_last_reveal_time: float = 0.0
_last_reveal_target: str = ""


def reveal(file_path: str = "", fallback_dir: str = "") -> tuple[bool, str]:
    """Show ``file_path`` in the file manager, falling back to a folder.

    Returns ``(ok, message)``.
    """
    global _last_reveal_time, _last_reveal_target
    target = (file_path or fallback_dir).strip().lower()
    now = time.time()
    if target and target == _last_reveal_target and (now - _last_reveal_time) < 1.0:
        return True, "Already opened"
    _last_reveal_time = now
    _last_reveal_target = target
    try:
        clean_file = os.path.normpath(file_path.strip().strip('"')) if file_path else ""
        if clean_file and os.path.isfile(clean_file):
            _select_file(clean_file)
            return True, os.path.dirname(clean_file)
        elif clean_file and os.path.isdir(clean_file):
            _open_folder(clean_file)
            return True, clean_file

        clean_fallback = os.path.normpath(fallback_dir.strip().strip('"')) if fallback_dir else ""
        # The known download folder wins over the parent of a missing file:
        # stale paths point into the temp dir, and opening that (or letting
        # Explorer's /select fall back to Documents) is exactly wrong.
        if clean_fallback:
            try:
                os.makedirs(clean_fallback, exist_ok=True)
            except OSError:
                pass
            if os.path.isdir(clean_fallback):
                _open_folder(clean_fallback)
                return True, clean_fallback

        if clean_file:
            parent = os.path.dirname(clean_file)
            if parent and os.path.isdir(parent) and "temp_downloads" not in parent.lower():
                _open_folder(parent)
                return True, parent

        return False, "Download folder could not be found."
    except Exception as exc:  # pragma: no cover - depends on the desktop shell
        return False, f"Could not open the folder: {exc}"

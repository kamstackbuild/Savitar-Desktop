"""Centralized robust clipboard reader for Savitar.

Provides multi-layer fallback clipboard text reading on Windows:
  Layer 1: Native Win32 API via ctypes with retry loop and proper Unicode extraction.
  Layer 2: PowerShell Windows.Forms.Clipboard (safe across all threads and COM states).
  Layer 3: PowerShell Get-Clipboard.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time

logger = logging.getLogger(__name__)


def get_clipboard_text(post_hotkey_delay: bool = False) -> str:
    """Read plain text from Windows clipboard with multi-layer fallback.

    Parameters
    ----------
    post_hotkey_delay : bool
        If True, sleeps 150ms before reading so modifier keys (Ctrl+Shift+D)
        have finished their key-up transitions.
    """
    if sys.platform != "win32":
        return ""

    if post_hotkey_delay:
        time.sleep(0.15)

    # ── Layer 1: Win32 native API with explicit types and retry loop ─────────
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = ctypes.c_bool
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = ctypes.c_bool
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
        user32.IsClipboardFormatAvailable.restype = ctypes.c_bool

        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_bool

        CF_UNICODETEXT = 13
        CF_TEXT = 1

        # Check if text format is in clipboard
        has_text = (
            user32.IsClipboardFormatAvailable(CF_UNICODETEXT)
            or user32.IsClipboardFormatAvailable(CF_TEXT)
        )
        if has_text:
            opened = False
            for _ in range(15):
                if user32.OpenClipboard(None):
                    opened = True
                    break
                time.sleep(0.04)

            if opened:
                try:
                    # Try CF_UNICODETEXT (Unicode UTF-16)
                    h_data = user32.GetClipboardData(CF_UNICODETEXT)
                    if h_data:
                        p_data = kernel32.GlobalLock(h_data)
                        if p_data:
                            try:
                                val = ctypes.c_wchar_p(p_data).value
                                if val and val.strip():
                                    return val.strip()
                            finally:
                                kernel32.GlobalUnlock(h_data)

                    # Fallback to CF_TEXT (ANSI)
                    h_data = user32.GetClipboardData(CF_TEXT)
                    if h_data:
                        p_data = kernel32.GlobalLock(h_data)
                        if p_data:
                            try:
                                raw = ctypes.c_char_p(p_data).value
                                if raw:
                                    decoded = raw.decode("utf-8", errors="ignore").strip()
                                    if decoded:
                                        return decoded
                            finally:
                                kernel32.GlobalUnlock(h_data)
                finally:
                    user32.CloseClipboard()
    except Exception as exc:
        logger.debug(f"Win32 clipboard read failed: {exc}")

    # ── Layer 2: PowerShell Windows.Forms.Clipboard ──────────────────────────
    try:
        res = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[System.Windows.Forms.Clipboard]::GetText()",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        if res.returncode == 0 and res.stdout and res.stdout.strip():
            return res.stdout.strip()
    except Exception as exc:
        logger.debug(f"PowerShell Forms clipboard failed: {exc}")

    # ── Layer 3: PowerShell Get-Clipboard ────────────────────────────────────
    try:
        res = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-Clipboard",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        if res.returncode == 0 and res.stdout and res.stdout.strip():
            return res.stdout.strip()
    except Exception as exc:
        logger.debug(f"PowerShell Get-Clipboard failed: {exc}")

    return ""

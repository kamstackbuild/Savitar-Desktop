"""Windows Startup (Autostart) Registry Manager for Savitar.

Manages the Run key under HKEY_CURRENT_USER to allow Savitar to start
automatically in the background (system tray) on Windows startup.
No admin privileges / UAC prompts required.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

REG_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_STARTUP_APPROVED_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
APP_REG_NAME = "Savitar"


def get_autostart_command() -> str:
    """Construct the exact command line to launch Savitar in the background."""
    if getattr(sys, "frozen", False):
        exe_path = str(Path(sys.executable).resolve()).strip('"')
        return f'"{exe_path}" --background'

    # Running from Python source
    py_exe = sys.executable
    # Prefer pythonw.exe if available so no command prompt console window ever appears
    pyw_candidate = Path(py_exe).parent / "pythonw.exe"
    if pyw_candidate.is_file():
        py_exe = str(pyw_candidate)

    py_exe = str(Path(py_exe).resolve()).strip('"')
    main_py = str((Path(__file__).resolve().parent.parent / "main.py").resolve()).strip('"')
    return f'"{py_exe}" "{main_py}" --background'


def is_run_key_present() -> bool:
    """Check if Savitar has a registered Run key in HKCU."""
    if sys.platform != "win32":
        return False

    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SUBKEY, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, APP_REG_NAME)
            return bool(val and str(val).strip())
    except OSError:
        return False
    except Exception as exc:
        logger.debug(f"is_run_key_present error: {exc}")
        return False


def is_task_manager_disabled() -> bool:
    """Check if Windows Task Manager StartupApproved has marked Savitar as disabled."""
    if sys.platform != "win32":
        return False

    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, REG_STARTUP_APPROVED_SUBKEY, 0, winreg.KEY_READ) as key:
                    val, _ = winreg.QueryValueEx(key, APP_REG_NAME)
                    if isinstance(val, (bytes, bytearray)) and len(val) > 0:
                        # In Windows StartupApproved binary blobs, bit 0 is set (0x01, 0x03) when disabled
                        if (val[0] & 1) != 0:
                            return True
            except OSError:
                continue
    except Exception as exc:
        logger.debug(f"is_task_manager_disabled error: {exc}")
    return False


def is_autostart_enabled() -> bool:
    """Check if Savitar is registered and actually enabled in Windows Startup."""
    return is_run_key_present() and not is_task_manager_disabled()


def get_autostart_info() -> dict:
    """Get complete autostart state including Task Manager approval state."""
    registered = is_run_key_present()
    tm_disabled = is_task_manager_disabled() if registered else False
    return {
        "registered": registered,
        "task_manager_disabled": tm_disabled,
        "enabled": registered and not tm_disabled,
        "command": get_autostart_command(),
    }


def set_autostart(enabled: bool) -> bool:
    """Enable or disable Savitar starting with Windows in the background."""
    if sys.platform != "win32":
        return False

    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SUBKEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                cmd = get_autostart_command()
                winreg.SetValueEx(key, APP_REG_NAME, 0, winreg.REG_SZ, cmd)
                logger.info(f"Windows autostart enabled: {cmd}")
            else:
                try:
                    winreg.DeleteValue(key, APP_REG_NAME)
                    logger.info("Windows autostart disabled.")
                except OSError:
                    # Key was already not present
                    pass

        # If user explicitly enabled autostart, attempt to reset/re-approve in StartupApproved if needed
        if enabled:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_STARTUP_APPROVED_SUBKEY, 0, winreg.KEY_SET_VALUE) as sa_key:
                    # Setting first byte to 0x02 marks it enabled in Windows 10/11 StartupApproved
                    winreg.SetValueEx(sa_key, APP_REG_NAME, 0, winreg.REG_BINARY, b"\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
            except OSError:
                pass
        return True
    except Exception as exc:
        logger.warning(f"Failed to set Windows autostart ({enabled}): {exc}")
        return False

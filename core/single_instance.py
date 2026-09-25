"""Single-instance protection and IPC activation for Savitar.

Ensures only one instance of Savitar runs at any given time. If a user tries to
launch Savitar while an instance is already running (or minimized in system tray),
the secondary instance signals the primary instance to restore its window to the
foreground, and then exits immediately.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MUTEX_NAME = "Local\\Savitar_SingleInstance_Mutex_D37F8A4B"
IPC_PORT = 48920
ERROR_ALREADY_EXISTS = 183


def _activate_existing_window_win32() -> bool:
    """Best-effort attempt to find and bring the existing Savitar window to front."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import ctypes.wintypes
        user32 = ctypes.windll.user32

        def is_explorer_or_tray(h):
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, cls_buf, 256)
            cls_val = cls_buf.value
            return cls_val in ("CabinetWClass", "ExploreWClass", "Progman", "WorkerW") or cls_val.startswith("SavitarTray_")

        # 1. Direct window title lookup (ignoring Explorer folder windows)
        hwnd = user32.FindWindowW(None, "Savitar")
        if hwnd and not is_explorer_or_tray(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True

        # 2. Enumerate top-level windows looking for 'Savitar'
        found_hwnd = None

        def enum_windows_proc(h, _):
            nonlocal found_hwnd
            if is_explorer_or_tray(h):
                return True
            length = user32.GetWindowTextLengthW(h)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(h, buff, length + 1)
                if "savitar" in buff.value.lower():
                    found_hwnd = h
                    return False
            return True

        enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(enum_proc_type(enum_windows_proc), 0)

        if found_hwnd:
            user32.ShowWindow(found_hwnd, 9)
            user32.SetForegroundWindow(found_hwnd)
            return True
    except Exception as exc:
        logger.debug(f"Win32 window activation error: {exc}")
    return False


def _send_ipc_show_signal(port: int = IPC_PORT) -> bool:
    """Send 'SHOW' activation command to primary instance's IPC listener."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.5)
            s.connect(("127.0.0.1", port))
            s.sendall(b"SHOW\n")
            return True
    except Exception as exc:
        logger.debug(f"IPC signal failed: {exc}")
        return False


class SingleInstance:
    """Guarantees a single running application instance using Win32 Mutex and IPC socket."""

    def __init__(
        self,
        on_show: Optional[Callable[[], None]] = None,
        mutex_name: Optional[str] = None,
        ipc_port: Optional[int] = None,
    ):
        self.on_show = on_show
        self.mutex_name = mutex_name or MUTEX_NAME
        self.ipc_port = ipc_port or IPC_PORT
        self.mutex_handle = None
        self._ipc_server = None
        self._ipc_thread: Optional[threading.Thread] = None
        self._is_primary = False

    def acquire(self) -> bool:
        """Attempt to acquire single-instance lock.

        Returns True if this is the primary instance, False if another instance is already running.
        """
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32

            # Create or open named mutex
            self.mutex_handle = kernel32.CreateMutexW(None, False, self.mutex_name)
            last_err = kernel32.GetLastError()

            if last_err == ERROR_ALREADY_EXISTS or not self.mutex_handle:
                # Another instance is already active
                logger.info("Existing Savitar instance detected. Signaling existing instance...")
                _send_ipc_show_signal(self.ipc_port)
                _activate_existing_window_win32()
                return False

        # Try to start IPC listener to confirm primary status
        try:
            self._start_ipc_server()
            self._is_primary = True
            return True
        except OSError:
            # Port in use or socket error: signal existing instance
            logger.info("IPC port in use. Signaling existing instance...")
            _send_ipc_show_signal(self.ipc_port)
            _activate_existing_window_win32()
            return False

    def _start_ipc_server(self):
        """Start local TCP server to listen for wake/show signals from duplicate launches."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", self.ipc_port))
        server.listen(5)
        server.settimeout(1.0)
        self._ipc_server = server

        def _listen_loop():
            while self._ipc_server:
                try:
                    conn, _ = self._ipc_server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                except Exception:
                    continue

                try:
                    with conn:
                        conn.settimeout(1.0)
                        data = conn.recv(64)
                        if b"SHOW" in data:
                            if self.on_show:
                                try:
                                    self.on_show()
                                except Exception as e:
                                    logger.debug(f"Error calling on_show: {e}")
                except Exception:
                    pass

        self._ipc_thread = threading.Thread(target=_listen_loop, daemon=True)
        self._ipc_thread.start()

    def set_on_show(self, on_show: Callable[[], None]):
        """Update the callback to trigger when a duplicate launch occurs."""
        self.on_show = on_show

    def release(self):
        """Release mutex and close IPC listener."""
        if self._ipc_server:
            try:
                srv = self._ipc_server
                self._ipc_server = None
                srv.close()
            except Exception:
                pass

        if sys.platform == "win32" and self.mutex_handle:
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(self.mutex_handle)
                self.mutex_handle = None
            except Exception:
                pass

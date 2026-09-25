"""Windows System Tray and Background Manager for Savitar.

Zero-dependency Windows Tray implementation using standard ctypes (Shell_NotifyIconW).
Supports:
- Window minimize to tray on close
- Windows Toast/Balloon notification when minimized to background
- Tray menu (Open Savitar, Exit)
- Left-click / Double-click to restore window
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import os
import sys
import threading
from typing import Callable, Optional

from core.binaries import app_dir, resource_path

logger = logging.getLogger(__name__)

# Windows Constants
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
WM_COMMAND = 0x0111
WM_HOTKEY = 0x0312
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_DESTROY = 0x0002

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

VK_D = 0x44
ID_HOTKEY_QUICK_DOWNLOAD = 2001

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_STATE = 0x00000008
NIF_INFO = 0x00000010

NIIF_INFO = 0x00000001
NIIF_USER = 0x00000004

TPM_BOTTOMALIGN = 0x0020
TPM_RIGHTALIGN = 0x0008
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800

ID_TRAY_OPEN = 1001
ID_TRAY_EXIT = 1002
ID_TRAY_QUICK_DOWNLOAD = 1003

WM_ENABLE_HOTKEY = WM_USER + 30
WM_DISABLE_HOTKEY = WM_USER + 31

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
VK_CONTROL = 0x11
VK_SHIFT = 0x10


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.wintypes.DWORD),
        ("scanCode", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long,
    ctypes.c_int,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("hWnd", ctypes.wintypes.HWND),
        ("uID", ctypes.wintypes.UINT),
        ("uFlags", ctypes.wintypes.UINT),
        ("uCallbackMessage", ctypes.wintypes.UINT),
        ("hIcon", ctypes.wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.wintypes.DWORD),
        ("dwStateMask", ctypes.wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeoutOrVersion", ctypes.wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", ctypes.wintypes.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long,
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.UINT),
        ("style", ctypes.wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.wintypes.HINSTANCE),
        ("hIcon", ctypes.wintypes.HICON),
        ("hCursor", ctypes.wintypes.HICON),
        ("hbrBackground", ctypes.wintypes.HBRUSH),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
        ("hIconSm", ctypes.wintypes.HICON),
    ]


def _setup_win32_signatures():
    """Explicitly set ctypes argtypes and restypes for 64-bit/32-bit Win32 APIs."""
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    shell32 = ctypes.windll.shell32

    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetModuleHandleW.restype = ctypes.wintypes.HINSTANCE

    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = ctypes.wintypes.ATOM

    user32.CreateWindowExW.argtypes = [
        ctypes.wintypes.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.wintypes.HWND, ctypes.wintypes.HMENU, ctypes.wintypes.HINSTANCE, ctypes.c_void_p
    ]
    user32.CreateWindowExW.restype = ctypes.wintypes.HWND

    user32.DefWindowProcW.argtypes = [
        ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
    ]
    user32.DefWindowProcW.restype = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long

    user32.CreatePopupMenu.restype = ctypes.wintypes.HMENU
    user32.CreatePopupMenu.argtypes = []

    user32.AppendMenuW.argtypes = [
        ctypes.wintypes.HMENU, ctypes.wintypes.UINT, ctypes.wintypes.UINT_PTR if hasattr(ctypes.wintypes, 'UINT_PTR') else ctypes.c_size_t, ctypes.c_wchar_p
    ]
    user32.AppendMenuW.restype = ctypes.wintypes.BOOL

    user32.TrackPopupMenu.argtypes = [
        ctypes.wintypes.HMENU, ctypes.wintypes.UINT, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.wintypes.HWND, ctypes.c_void_p
    ]
    user32.TrackPopupMenu.restype = ctypes.c_int

    user32.DestroyMenu.argtypes = [ctypes.wintypes.HMENU]
    user32.DestroyMenu.restype = ctypes.wintypes.BOOL

    user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
    user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL

    user32.GetCursorPos.argtypes = [ctypes.POINTER(ctypes.wintypes.POINT)]
    user32.GetCursorPos.restype = ctypes.wintypes.BOOL

    user32.PostMessageW.argtypes = [
        ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
    ]
    user32.PostMessageW.restype = ctypes.wintypes.BOOL

    user32.RegisterHotKey.argtypes = [
        ctypes.wintypes.HWND, ctypes.c_int, ctypes.wintypes.UINT, ctypes.wintypes.UINT
    ]
    user32.RegisterHotKey.restype = ctypes.wintypes.BOOL

    user32.UnregisterHotKey.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = ctypes.wintypes.BOOL

    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.wintypes.HINSTANCE, ctypes.wintypes.DWORD]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p

    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.UnhookWindowsHookEx.restype = ctypes.wintypes.BOOL

    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
    user32.CallNextHookEx.restype = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long

    user32.GetKeyState.argtypes = [ctypes.c_int]
    user32.GetKeyState.restype = ctypes.c_short

    shell32.Shell_NotifyIconW.argtypes = [ctypes.wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = ctypes.wintypes.BOOL


if sys.platform == "win32":
    try:
        _setup_win32_signatures()
    except Exception as e:
        logger.debug(f"Win32 signatures init warning: {e}")


class WindowsTrayIcon:
    """Manages the Windows System Tray icon, background notifications, and global hotkeys."""

    def __init__(
        self,
        title: str = "Savitar",
        on_open: Optional[Callable[[], None]] = None,
        on_exit: Optional[Callable[[], None]] = None,
        on_quick_download: Optional[Callable[[], None]] = None,
        quick_download_enabled: bool = True,
    ):
        self.title = title
        self.on_open = on_open
        self.on_exit = on_exit
        self.on_quick_download = on_quick_download
        self.quick_download_enabled = quick_download_enabled
        self.hwnd: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._wndproc_ref = None
        self._hotkey_registered = False
        self._hook_handle = None
        self._hook_proc_ref = None
        self._last_hotkey_time = 0.0

    def start(self):
        """Start the tray message loop in a dedicated thread."""
        if sys.platform != "win32":
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def show_notification(self, title: str, message: str):
        """Display a Windows balloon / toast notification."""
        if sys.platform != "win32" or not self.hwnd:
            return
        try:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = NIF_INFO
            nid.szInfo = message[:255]
            nid.szInfoTitle = title[:63]
            nid.dwInfoFlags = NIIF_INFO
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception as e:
            logger.debug(f"Failed to show notification: {e}")

    def _register_hotkey(self, hwnd=None):
        target_hwnd = hwnd or self.hwnd
        if sys.platform != "win32":
            return
        if self._hotkey_registered:
            return
        try:
            user32 = ctypes.windll.user32
            ok = False
            if target_hwnd:
                ok = user32.RegisterHotKey(
                    target_hwnd,
                    ID_HOTKEY_QUICK_DOWNLOAD,
                    MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT,
                    VK_D,
                )
                if not ok:
                    ok = user32.RegisterHotKey(
                        target_hwnd,
                        ID_HOTKEY_QUICK_DOWNLOAD,
                        MOD_CONTROL | MOD_SHIFT,
                        VK_D,
                    )
            if ok:
                self._hotkey_registered = True
                logger.info("Registered global quick-download hotkey (Ctrl + Shift + D)")
            else:
                self._install_keyboard_hook()
        except Exception as e:
            logger.debug(f"RegisterHotKey exception: {e}")
            self._install_keyboard_hook()

    def _install_keyboard_hook(self):
        if sys.platform != "win32" or self._hook_handle or not self.quick_download_enabled:
            return
        user32 = ctypes.windll.user32

        def _ll_keyboard_proc(nCode, wParam, lParam):
            if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                try:
                    vk_code = ctypes.cast(lParam, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value
                    if vk_code in (ord('D'), ord('d'), 0x44):
                        ctrl_down = bool(user32.GetKeyState(VK_CONTROL) & 0x8000)
                        shift_down = bool(user32.GetKeyState(VK_SHIFT) & 0x8000)
                        if ctrl_down and shift_down:
                            import time
                            now = time.time()
                            if now - self._last_hotkey_time > 1.2:
                                self._last_hotkey_time = now
                                if self.on_quick_download:
                                    threading.Thread(target=self.on_quick_download, daemon=True).start()
                except Exception as exc:
                    logger.debug(f"Keyboard hook error: {exc}")
            return user32.CallNextHookEx(self._hook_handle, nCode, wParam, lParam)

        try:
            self._hook_proc_ref = HOOKPROC(_ll_keyboard_proc)
            h_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_proc_ref, None, 0)
            if h_hook:
                self._hook_handle = h_hook
                self._hotkey_registered = True
                logger.info("Installed resilient keyboard hook for Ctrl + Shift + D")
        except Exception as exc:
            logger.debug(f"Failed to install keyboard hook: {exc}")

    def _unregister_hotkey(self, hwnd=None):
        target_hwnd = hwnd or self.hwnd
        if sys.platform != "win32":
            return
        user32 = ctypes.windll.user32
        if self._hook_handle:
            try:
                user32.UnhookWindowsHookEx(self._hook_handle)
            except Exception:
                pass
            self._hook_handle = None
            self._hook_proc_ref = None
        if target_hwnd and self._hotkey_registered:
            try:
                user32.UnregisterHotKey(target_hwnd, ID_HOTKEY_QUICK_DOWNLOAD)
            except Exception:
                pass
        self._hotkey_registered = False
        logger.info("Unregistered quick-download hotkey / hook")

    def set_quick_download_enabled(self, enabled: bool):
        """Dynamically enable or disable the Ctrl + Shift + D global hotkey across threads."""
        self.quick_download_enabled = enabled
        if sys.platform != "win32":
            return
        if self.hwnd and self._thread and self._thread.is_alive():
            user32 = ctypes.windll.user32
            msg = WM_ENABLE_HOTKEY if enabled else WM_DISABLE_HOTKEY
            user32.PostMessageW(self.hwnd, msg, 0, 0)
        else:
            if enabled:
                self._register_hotkey()
            else:
                self._unregister_hotkey()

    def stop(self):
        """Remove the tray icon and clean up resources."""
        self._running = False
        if sys.platform != "win32" or not self.hwnd:
            return
        try:
            self._unregister_hotkey()
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = 1
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            ctypes.windll.user32.PostMessageW(self.hwnd, WM_DESTROY, 0, 0)
        except Exception:
            pass

    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAYICON:
                event = lparam & 0xFFFF
                if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    if self.on_open:
                        self.on_open()
                elif event in (WM_RBUTTONUP, WM_RBUTTONDOWN, 0x007B):
                    self._show_context_menu(hwnd)
                return 0
            elif msg == WM_HOTKEY:
                cmd_id = wparam & 0xFFFF
                if cmd_id == ID_HOTKEY_QUICK_DOWNLOAD:
                    if self.on_quick_download:
                        try:
                            self.on_quick_download()
                        except Exception as exc:
                            logger.debug(f"Error in on_quick_download: {exc}")
                return 0
            elif msg == WM_ENABLE_HOTKEY:
                self._register_hotkey(hwnd)
                return 0
            elif msg == WM_DISABLE_HOTKEY:
                self._unregister_hotkey(hwnd)
                return 0
            elif msg == WM_COMMAND:
                cmd_id = wparam & 0xFFFF
                if cmd_id == ID_TRAY_OPEN:
                    if self.on_open:
                        self.on_open()
                elif cmd_id == ID_TRAY_EXIT:
                    if self.on_exit:
                        self.on_exit()
                return 0
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = WNDPROC(wnd_proc)

        hinstance = kernel32.GetModuleHandleW(None)
        class_name = f"SavitarTray_{os.getpid()}"

        icon_handle = 0
        for cand in (
            str(resource_path("assets/icons/icon.ico")),
            str(resource_path("static/icon.ico")),
            str(resource_path("icon.ico")),
            str(app_dir() / "assets" / "icons" / "icon.ico"),
            str(app_dir() / "static" / "icon.ico"),
            str(app_dir() / "icon.ico"),
        ):
            if os.path.isfile(cand):
                try:
                    # LR_LOADFROMFILE = 0x0010, IMAGE_ICON = 1
                    icon_handle = user32.LoadImageW(0, cand, 1, 0, 0, 0x0010)
                    if icon_handle:
                        break
                except Exception:
                    pass
        if not icon_handle:
            icon_handle = user32.LoadIconW(0, 32512)
        self.h_icon = icon_handle

        wndclass = WNDCLASSEXW()
        wndclass.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wndclass.lpfnWndProc = self._wndproc_ref
        wndclass.hInstance = hinstance
        wndclass.lpszClassName = class_name
        wndclass.hIcon = icon_handle

        user32.RegisterClassExW(ctypes.byref(wndclass))

        hwnd = user32.CreateWindowExW(
            0, class_name, class_name, 0, 0, 0, 0, 0, 0, 0, hinstance, 0
        )
        self.hwnd = hwnd

        # Register tray icon
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = icon_handle
        nid.szTip = self.title[:127]

        ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

        if self.quick_download_enabled:
            self._register_hotkey(hwnd)

        msg = ctypes.wintypes.MSG()
        while self._running and user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _show_context_menu(self, hwnd):
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        if not menu:
            return

        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_OPEN, "Open Savitar")
        if self.on_quick_download and self.quick_download_enabled:
            user32.AppendMenuW(menu, MF_STRING, ID_TRAY_QUICK_DOWNLOAD, "Quick Download Copied Link (Ctrl+Shift+D)")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, "")
        user32.AppendMenuW(menu, MF_STRING, ID_TRAY_EXIT, "Quit Savitar")

        pt = ctypes.wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)

        # TrackPopupMenu with TPM_RIGHTBUTTON | TPM_RETURNCMD
        cmd = user32.TrackPopupMenu(
            menu,
            0x0002 | 0x0100,
            pt.x,
            pt.y,
            0,
            hwnd,
            None,
        )
        user32.PostMessageW(hwnd, 0, 0, 0)
        user32.DestroyMenu(menu)

        if cmd == ID_TRAY_OPEN:
            if self.on_open:
                self.on_open()
        elif cmd == ID_TRAY_QUICK_DOWNLOAD:
            if self.on_quick_download:
                threading.Thread(target=self.on_quick_download, daemon=True).start()
        elif cmd == ID_TRAY_EXIT:
            if self.on_exit:
                self.on_exit()

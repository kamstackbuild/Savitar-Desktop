import io
import multiprocessing
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time

# ── 1. Freeze support for multiprocessing in PyInstaller bundles ──────────────
multiprocessing.freeze_support()

# ── 2. Intercept yt-dlp CLI delegation when running as frozen binary ─────────
# When downloader/extractor invokes [sys.executable, "-m", "yt_dlp", ...], handle
# it directly as a CLI tool without starting the GUI or background web server!
if len(sys.argv) > 2 and sys.argv[1] == "-m" and sys.argv[2] == "yt_dlp":
    import yt_dlp
    sys.exit(yt_dlp.main(sys.argv[3:]))

# ── 2. Safe Stream Wrappers for Windowed (console=False) Mode ─────────────────
class _SafeNullStream:
    def write(self, s):
        pass
    def flush(self):
        pass
    def isatty(self):
        return False
    def fileno(self):
        return -1

if sys.stdout is None or not hasattr(sys.stdout, 'isatty'):
    sys.stdout = _SafeNullStream()
if sys.stderr is None or not hasattr(sys.stderr, 'isatty'):
    sys.stderr = _SafeNullStream()
if sys.stdin is None:
    sys.stdin = io.StringIO()

_startup_error = []


def log_startup(message: str, exc: Exception | None = None):
    """Write timestamped startup diagnostics to %APPDATA%\\Savitar\\startup.log."""
    try:
        try:
            home_str = str(pathlib.Path.home())
        except Exception:
            import tempfile
            home_str = tempfile.gettempdir()
        appdata = os.environ.get('APPDATA') or os.environ.get('LOCALAPPDATA') or home_str
        log_dir = pathlib.Path(appdata) / 'Savitar'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / 'startup.log'
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"[{ts}] {message}\n")
            if exc:
                import traceback
                f.write(traceback.format_exc() + "\n")
    except Exception:
        pass

def check_webview2_installed() -> bool:
    """Verify that Microsoft Edge WebView2 Evergreen Runtime is installed."""
    if sys.platform != "win32":
        return True

    # 1. Direct filesystem check (most reliable across Windows 10/11)
    for folder in (
        r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application",
        r"C:\Program Files\Microsoft\EdgeWebView\Application",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\EdgeWebView\Application"),
    ):
        if os.path.isdir(folder):
            try:
                for entry in os.listdir(folder):
                    if entry and (entry[0].isdigit() or os.path.isfile(os.path.join(folder, entry, "msedgewebview2.exe"))):
                        return True
            except Exception:
                pass

    # 2. Registry paths (Clients & Uninstall)
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (
                r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
                r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
                r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",
                r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",
                r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-F38E-4497-9A17-DEE80C309F0E}",
                r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-F38E-4497-9A17-DEE80C309F0E}",
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Microsoft Edge WebView2 Runtime",
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Microsoft Edge WebView2 Runtime",
            ):
                try:
                    with winreg.OpenKey(root, sub) as key:
                        for val_name in ("pv", "DisplayVersion", "Version"):
                            try:
                                val, _ = winreg.QueryValueEx(key, val_name)
                                if val and str(val).strip() not in ("", "0.0.0.0"):
                                    return True
                            except OSError:
                                pass
                except OSError:
                    pass
    except Exception:
        pass
    return False

def is_webview2_usable() -> bool:
    """Verify that pywebview will actually use EdgeChromium rather than dead MSHTML."""
    if sys.platform != "win32":
        return True
    try:
        import webview.platforms.winforms as wf
        return bool(getattr(wf, "is_chromium", False) and getattr(wf, "renderer", "") != "mshtml")
    except Exception as exc:
        log_startup(f"is_webview2_usable check failed: {exc}")
        return False

def ensure_webview2_ready(retries: int = 3, delay: float = 1.0) -> bool:
    """Check WebView2; if missing, attempt silent background install of bundled bootstrapper."""
    for attempt in range(retries):
        if check_webview2_installed():
            return True
        if attempt < retries - 1:
            time.sleep(delay)

    from core.binaries import app_dir, resource_path
    candidates = [
        str(resource_path("installer/redist/MicrosoftEdgeWebView2RuntimeInstallerX64.exe")),
        str(app_dir() / "installer" / "redist" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"),
        str(app_dir() / "redist" / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"),
        str(resource_path("MicrosoftEdgeWebview2Setup.exe")),
        str(app_dir() / "MicrosoftEdgeWebview2Setup.exe"),
        str(app_dir() / "bin" / "MicrosoftEdgeWebview2Setup.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "MicrosoftEdgeWebview2Setup.exe"),
    ]

    for installer in candidates:
        if os.path.isfile(installer):
            try:
                proc = subprocess.run([installer, "/silent", "/install"], capture_output=True, timeout=120)
                if proc.returncode == 0 and check_webview2_installed():
                    return True
            except Exception as exc:
                log_startup(f"WebView2 silent installer error: {exc}", exc=exc)
    return check_webview2_installed()

def show_webview2_missing_dialog():
    """Prompt user to download official Microsoft Edge WebView2 runtime if missing."""
    try:
        import ctypes
        import webbrowser
        MB_YESNO = 0x04
        MB_ICONWARNING = 0x30
        IDYES = 6
        msg = (
            "Microsoft Edge WebView2 Runtime is required to run Savitar, "
            "but it was not found on this computer.\n\n"
            "Would you like to open Microsoft's official download page to install it now?"
        )
        res = ctypes.windll.user32.MessageBoxW(0, msg, "Savitar - Component Required", MB_YESNO | MB_ICONWARNING)
        if res == IDYES:
            webbrowser.open("https://go.microsoft.com/fwlink/p/?LinkId=2124703")
    except Exception:
        pass

def find_free_port(max_retries: int = 5, backoff_base: float = 0.5) -> int:
    """Find an available loopback port with retries and backoff for early Windows logon."""
    for attempt in range(max_retries):
        for test_port in (48921, 48922, 48923, 48924, 48925):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('127.0.0.1', test_port))
                    return test_port
            except OSError:
                continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', 0))
                return s.getsockname()[1]
        except OSError as bind_err:
            log_startup(f"find_free_port attempt {attempt + 1}/{max_retries} failed: {bind_err}")
            if attempt < max_retries - 1:
                time.sleep(backoff_base * (1.5 ** attempt))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

_app_instance = None

from core.clipboard import get_clipboard_text as get_native_clipboard_text



def start_server(port: int):
    global _app_instance
    try:
        import uvicorn
        from server import create_app
        _app_instance = create_app()
        config = uvicorn.Config(
            app=_app_instance,
            host='127.0.0.1',
            port=port,
            log_config=None,
            log_level='critical',
            access_log=False,
            use_colors=False,
        )
        server = uvicorn.Server(config)
        server.run()
    except Exception as exc:
        import traceback
        err_msg = traceback.format_exc()
        _startup_error.append(err_msg)
        log_startup(f"start_server exception on port {port}: {exc}", exc=exc)
        try:
            log_path = pathlib.Path(os.environ.get('LOCALAPPDATA', '')) / 'Savitar' / 'startup_error.log'
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(err_msg, encoding='utf-8')
        except Exception:
            pass

def wait_for_server(port: int, timeout: float = 25.0):
    start = time.time()
    while time.time() - start < timeout:
        if _startup_error:
            log_startup(f"wait_for_server detected startup error on port {port}: {_startup_error[0]}")
            return False
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    log_startup(f"wait_for_server timed out waiting for port {port} after {timeout}s")
    return False

def _run_main():
    from core.binaries import data_dir, resource_root
    base_dir = resource_root()
    sys.path.insert(0, str(base_dir))

    # In frozen mode, change working directory to user data_dir to prevent writing beside the exe
    if getattr(sys, 'frozen', False):
        try:
            d = data_dir()
            d.mkdir(parents=True, exist_ok=True)
            os.chdir(str(d))
        except Exception:
            pass
    else:
        os.chdir(str(base_dir))

    from core.single_instance import SingleInstance
    single_instance = SingleInstance()
    if not single_instance.acquire():
        # Another instance is already running; it was signaled to restore its window.
        log_startup("Existing Savitar instance detected; primary activated, exiting this launch.")
        sys.exit(0)

    ensure_webview2_ready()

    from core.settings import AppSettings
    from core.tray import WindowsTrayIcon

    try:
        settings = AppSettings.load()
    except Exception as st_err:
        log_startup(f"Failed to load AppSettings: {st_err}")
        class DummySettings:
            window_width = 1280
            window_height = 800
        settings = DummySettings()

    port = find_free_port()
    log_startup(f"Allocated server port: {port}")

    server_thread = threading.Thread(target=start_server, args=(port,), daemon=True)
    server_thread.start()

    if not wait_for_server(port, timeout=25.0):
        err_detail = _startup_error[0] if _startup_error else "Could not bind to 127.0.0.1 or port in use."
        log_startup(f"Savitar backend server failed to initialize: {err_detail}")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                f"Savitar backend server failed to initialize:\n\n{err_detail[:250]}",
                "Savitar Startup Error",
                0x10,
            )
        except Exception:
            pass
        sys.exit(1)

    if not is_webview2_usable():
        log_startup("Fatal: Microsoft Edge WebView2 runtime is missing or unusable.")
        try:
            import ctypes
            msg = (
                "Microsoft Edge WebView2 Runtime is required to run Savitar.\n\n"
                "Please run the Savitar Setup installer or download the WebView2 Runtime from Microsoft."
            )
            ctypes.windll.user32.MessageBoxW(0, msg, "Savitar - Component Required", 0x10)
        except Exception:
            pass
        sys.exit(1)

    is_background = ("--background" in sys.argv or "--minimized" in sys.argv)
    window = None

    try:
        import webview
        window = webview.create_window(
            'Savitar',
            url=f'http://127.0.0.1:{port}',
            width=getattr(settings, 'window_width', 1280),
            height=getattr(settings, 'window_height', 800),
            min_size=(800, 600),
            resizable=True,
            text_select=True,
            hidden=is_background,
            maximized=False,
        )
    except Exception as cw_err:
        log_startup(f"webview.create_window error: {cw_err}")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                f"Failed to create Savitar application window:\n\n{cw_err}",
                "Savitar Startup Error",
                0x10,
            )
        except Exception:
            pass
        sys.exit(1)

    is_quitting = False
    tray_icon = None

    def on_restore():
        if window:
            try:
                window.show()
                window.restore()
                window.maximize()
            except Exception:
                pass
            try:
                import ctypes
                import ctypes.wintypes
                user32 = ctypes.windll.user32
                my_pid = os.getpid()
                target_hwnd = None

                def _find_my_win(h, _):
                    nonlocal target_hwnd
                    pid = ctypes.wintypes.DWORD()
                    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                    if pid.value == my_pid:
                        cls_buf = ctypes.create_unicode_buffer(256)
                        user32.GetClassNameW(h, cls_buf, 256)
                        if not cls_buf.value.startswith("SavitarTray_"):
                            target_hwnd = h
                            return False
                    return True

                PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
                user32.EnumWindows(PROC(_find_my_win), 0)

                if target_hwnd:
                    user32.ShowWindow(target_hwnd, 9)
                    user32.SetForegroundWindow(target_hwnd)
                    if tray_icon and getattr(tray_icon, "h_icon", 0):
                        user32.SendMessageW(target_hwnd, 0x0080, 1, tray_icon.h_icon)
                        user32.SendMessageW(target_hwnd, 0x0080, 0, tray_icon.h_icon)
            except Exception:
                pass

    single_instance.set_on_show(on_restore)

    def on_exit():
        nonlocal is_quitting
        is_quitting = True
        try:
            single_instance.release()
        except Exception:
            pass
        if tray_icon:
            tray_icon.stop()
        if window:
            try:
                window.destroy()
            except Exception:
                pass
        os._exit(0)

    def handle_quick_download():
        # post_hotkey_delay=True: waits for Ctrl/Shift keys to release before
        # accessing clipboard — fixes "empty clipboard" false positive on hotkey trigger
        text = (get_native_clipboard_text(post_hotkey_delay=True) or "").strip()
        if not text:
            if tray_icon:
                tray_icon.show_notification("Savitar Quick Download", "Clipboard is empty. Copy a video link first.")
            return

        url = text
        if not (url.startswith("http://") or url.startswith("https://") or "://" in url):
            if any(d in url.lower() for d in ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "twitter.com", "x.com", "facebook.com", "fb.watch", "reddit.com", "pin.it", "pinterest.com")):
                url = "https://" + url
            else:
                if tray_icon:
                    tray_icon.show_notification("Savitar Quick Download", "Copied text is not a recognized video link.")
                return

        if tray_icon:
            tray_icon.show_notification("Savitar Quick Download", f"Processing video link...\n{url[:45]}...")

        def _bg_quick_download():
            try:
                _execute_quick_download(url, port)
            except Exception as bg_err:
                if tray_icon:
                    tray_icon.show_notification("Savitar Quick Download", f"Error: {bg_err}")

        threading.Thread(target=_bg_quick_download, daemon=True).start()

    def _execute_quick_download(media_url: str, server_port: int):
        import urllib.request
        import json
        from core.settings import AppSettings

        st = AppSettings.load()
        extract_url = f"http://127.0.0.1:{server_port}/api/extract"
        req_data = json.dumps({"url": media_url}).encode("utf-8")
        req = urllib.request.Request(
            extract_url,
            data=req_data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            if tray_icon:
                tray_icon.show_notification("Savitar Quick Download", f"Could not extract video: {exc}")
            return

        if not data.get("ok") or not data.get("formats"):
            msg = data.get("message") or "No downloadable streams found."
            if tray_icon:
                tray_icon.show_notification("Savitar Quick Download", msg)
            return

        formats = data.get("formats", [])
        chosen = None
        pq = (getattr(st, "preferred_quality", "best") or "best").lower().strip()

        presets = [f for f in formats if f.get("kind") == "preset"]
        candidates = presets if presets else formats

        if pq == "audio":
            for f in candidates:
                if f.get("audio_only") or f.get("tier") == "audio":
                    chosen = f
                    break
        elif pq in ("best", "max"):
            for f in candidates:
                lbl = (f.get("label") or "").lower()
                if "max quality" in lbl or f.get("recommended"):
                    chosen = f
                    break
            if not chosen and candidates:
                chosen = max(candidates, key=lambda f: (f.get("height", 0) or 0))
        else:
            target_h = int(pq.replace("p", "")) if pq.endswith("p") and pq[:-1].isdigit() else 0
            if target_h:
                best_match = None
                best_diff = 99999
                for f in candidates:
                    h = f.get("height", 0) or 0
                    sublabel = (f.get("sublabel") or "").lower()
                    if sublabel == pq or h == target_h:
                        chosen = f
                        break
                    if 0 < h <= target_h:
                        diff = target_h - h
                        if diff < best_diff:
                            best_diff = diff
                            best_match = f
                if not chosen and best_match:
                    chosen = best_match

        if not chosen:
            chosen = next((f for f in candidates if f.get("recommended")), None)
        if not chosen:
            for f in formats:
                if f.get("quality") in ("1080p", "720p", "best") and f.get("has_audio", True):
                    chosen = f
                    break
        if not chosen:
            chosen = formats[0]

        format_id = chosen.get("format_id", "")
        if not format_id or format_id in ("best", "max"):
            format_id = "bestvideo+bestaudio/best"
        needs_merge = bool(chosen.get("needs_merge")) or ("+" in format_id)
        direct_url = chosen.get("url", "") if (chosen.get("url") and not needs_merge and chosen.get("has_audio") is not False and "+" not in format_id) else ""

        dl_payload = {
            "video_url": media_url,
            "format_id": format_id,
            "direct_url": direct_url or "",
            "needs_merge": bool(chosen.get("needs_merge")),
            "has_audio": chosen.get("has_audio") is not False,
            "title": data.get("title") or "Video",
            "format_label": chosen.get("label") or chosen.get("sublabel") or "Video",
            "thumbnail": data.get("thumbnail") or "",
            "platform": data.get("source_platform") or "video",
            "quality": chosen.get("sublabel") or (chosen.get("label", "").split()[0] if chosen.get("label") else ""),
            "duration": int(data.get("duration_seconds") or 0),
        }

        dl_url = f"http://127.0.0.1:{server_port}/api/download"
        dl_data = json.dumps(dl_payload).encode("utf-8")
        dl_req = urllib.request.Request(
            dl_url,
            data=dl_data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(dl_req, timeout=15) as dl_resp:
                res_dl = json.loads(dl_resp.read().decode("utf-8"))
        except Exception as dl_err:
            if tray_icon:
                tray_icon.show_notification("Savitar Quick Download", f"Download start error: {dl_err}")
            return

        if tray_icon:
            title = data.get("title", "Video")
            tray_icon.show_notification("Download Started", f"{title[:50]}")

    if _app_instance and hasattr(_app_instance.state, "download_manager"):
        def _on_task_done(t):
            if tray_icon:
                tray_icon.show_notification("Download Complete", f"{t.title[:50]} finished!")
        _app_instance.state.download_manager.on_task_completed = _on_task_done

    tray_icon = WindowsTrayIcon(
        title="Savitar - Video Downloader",
        on_open=on_restore,
        on_exit=on_exit,
        on_quick_download=handle_quick_download,
        quick_download_enabled=getattr(settings, "quick_download_shortcut", True),
    )
    if _app_instance:
        _app_instance.state.tray_icon = tray_icon
    tray_icon.start()

    def on_closing():
        nonlocal is_quitting
        if is_quitting:
            return True
        try:
            window.hide()
            tray_icon.show_notification(
                "Savitar",
                "Savitar is running in the background. Downloads will continue.",
            )
        except Exception:
            pass
        return False

    window.events.closing += on_closing

    should_maximize = not is_background
    def on_shown():
        if should_maximize:
            try:
                threading.Timer(0.15, window.maximize).start()
            except Exception:
                pass

    window.events.shown += on_shown

    localappdata = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or str(pathlib.Path.home())
    webview_data_dir = pathlib.Path(localappdata) / 'Savitar' / 'webview_data'
    try:
        webview_data_dir.mkdir(parents=True, exist_ok=True)
        lock_file = webview_data_dir / 'EBWebView' / 'SingletonLock'
        if lock_file.exists():
            lock_file.unlink(missing_ok=True)
    except Exception:
        import tempfile
        webview_data_dir = pathlib.Path(tempfile.gettempdir()) / 'Savitar_webview'
        webview_data_dir.mkdir(parents=True, exist_ok=True)

    try:
        import webview
        webview.start(
            debug=not getattr(sys, "frozen", False),
            private_mode=False,
            storage_path=str(webview_data_dir),
            gui='edgechromium',
        )
    except Exception as w_exc:
        import traceback
        err = traceback.format_exc()
        log_startup(f"webview.start fatal error: {err}")
        try:
            log_path = pathlib.Path(os.environ.get('LOCALAPPDATA', '')) / 'Savitar' / 'webview_error.log'
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(err, encoding='utf-8')
        except Exception:
            pass
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                f"Savitar encountered a fatal display error:\n\n{w_exc}",
                "Savitar Fatal Error",
                0x10,
            )
        except Exception:
            pass
    finally:
        try:
            single_instance.release()
        except Exception:
            pass
        if tray_icon:
            tray_icon.stop()


def main():
    try:
        log_startup(f"Savitar process started (PID={os.getpid()}, argv={sys.argv})")
        _run_main()
    except SystemExit:
        raise
    except Exception as exc:
        log_startup(f"Unhandled fatal startup error: {exc}", exc=exc)
        raise


if __name__ == '__main__':
    main()

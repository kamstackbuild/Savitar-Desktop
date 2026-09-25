import asyncio
import json
import logging
import os
import pathlib
import sys
import dataclasses
from datetime import datetime

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.binaries import resource_root, resource_path
BASE_DIR = resource_root()
sys.path.insert(0, str(BASE_DIR))

from core.extractor import (
    extract as run_extract,
    extract_playlist,
    is_playlist_or_channel_url,
    ExtractResult,
    MediaFormat,
    PlaylistExtractResult,
    PlaylistEntry,
)
from core.errors import ErrorCode, ExtractError
from core.platforms import public_platforms, platform_folder_name
from core.downloader import DownloadManager, DownloadTask, _guess_output
from core.cookies import detect_installed_browsers
from core.reveal import reveal as reveal_in_file_manager
from core.updater import (
    check_and_update,
    engine_status,
    get_aria2_version,
    get_ytdlp_version,
    update_aria2c,
    update_all,
    update_ffmpeg,
    update_ytdlp,
)
from core.app_updater import check_app_update, APP_VERSION
import webbrowser
from core.settings import AppSettings

class ExtractRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    video_url: str
    format_id: str
    title: str
    format_label: str = ''
    thumbnail: str = ''
    platform: str = ''
    quality: str = ''
    duration: int = 0
    direct_url: str = ''
    needs_merge: bool = False
    has_audio: bool = True
    total_bytes: int = 0

class PlaylistExtractRequest(BaseModel):
    url: str
    max_items: int = 0

class PlaylistItemDownload(BaseModel):
    video_url: str
    title: str
    thumbnail: str = ''
    duration: int = 0
    index: int = 0

class PlaylistDownloadRequest(BaseModel):
    playlist_title: str
    playlist_id: str = ''
    format_id: str
    format_label: str = ''
    subfolder: bool = True
    numbered: bool = True
    sequential: bool = False
    items: list[PlaylistItemDownload]

class ConvertRequest(BaseModel):
    source_path: str
    target_format: str
    output_dir: str = ''

class QueueMoveRequest(BaseModel):
    download_id: str
    direction: str

class QueueReorderRequest(BaseModel):
    task_ids: list[str]

def serialize_format(f) -> dict:
    d = dataclasses.asdict(f) if dataclasses.is_dataclass(f) else f.__dict__.copy()
    if d.get("expires_at"):
        if isinstance(d["expires_at"], datetime):
            d["expires_at"] = d["expires_at"].isoformat()
    return d

def serialize_extract_result(res) -> dict:
    # Build from dataclass fields first
    d = dataclasses.asdict(res) if dataclasses.is_dataclass(res) else res.__dict__.copy()
    if hasattr(res, "formats"):
        d["formats"] = [serialize_format(f) for f in res.formats]
    # Expose 'duration' key (alias of duration_seconds) for the frontend
    d["duration"] = d.get("duration_seconds") or 0
    # Expose 'platform' key
    d["platform"] = d.get("source_platform", "")
    d["extractor"] = d.get("source_platform", "")
    return d

def serialize_playlist_result(res: PlaylistExtractResult) -> dict:
    return dataclasses.asdict(res) if dataclasses.is_dataclass(res) else res.__dict__.copy()

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(check_and_update())
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Savitar API", docs_url=None, redoc_url=None, lifespan=lifespan)

    app.state.settings = AppSettings.load()
    app.state.download_manager = DownloadManager()
    if hasattr(app.state.download_manager, "rate_limiter"):
        app.state.download_manager.rate_limiter.set_speed_limit(app.state.settings.speed_limit_kbps)

    @app.middleware("http")
    async def validate_host_middleware(request: Request, call_next):
        raw_host = request.headers.get("host", "").strip().lower()
        if not raw_host:
            return JSONResponse(
                status_code=403,
                content={"ok": False, "message": "Access forbidden: missing host header."}
            )
        host_name = raw_host.split(":")[0]
        if host_name not in ("127.0.0.1", "localhost", "::1", "testserver", "test"):
            return JSONResponse(
                status_code=403,
                content={"ok": False, "message": "Access forbidden: untrusted host header."}
            )
        return await call_next(request)

    @app.middleware("http")
    async def no_cache_static_middleware(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    static_dir = resource_path("static")
    try:
        static_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError):
        pass
    app.mount('/static', StaticFiles(directory=str(static_dir)), name='static')

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logging.error(f"Unhandled exception: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "code": "TEMPORARY_FAILURE", "message": "Something went wrong."}
        )

    @app.get("/")
    async def index():
        index_path = static_dir / "index.html"
        if not index_path.exists():
            try:
                index_path.write_text("<html><body>Savitar Desktop App</body></html>", encoding="utf-8")
            except (OSError, PermissionError):
                pass
        return FileResponse(index_path)

    @app.get("/favicon.ico")
    async def favicon_ico():
        return FileResponse(static_dir / "favicon.ico")

    @app.get("/favicon.svg")
    async def favicon_svg():
        return FileResponse(static_dir / "favicon.svg")

    @app.get("/favicon-96x96.png")
    async def favicon_96():
        return FileResponse(static_dir / "favicon-96x96.png")

    @app.get("/apple-touch-icon.png")
    async def apple_touch_icon():
        return FileResponse(static_dir / "apple-touch-icon.png")

    @app.get("/site.webmanifest")
    async def site_manifest():
        return FileResponse(static_dir / "site.webmanifest")

    @app.get("/api/platforms")
    async def get_platforms():
        return {"ok": True, "platforms": public_platforms()}

    @app.post("/api/extract")
    async def extract_url(req: ExtractRequest):
        try:
            settings = app.state.settings
            result = await run_extract(
                req.url,
                cookies_browser=settings.cookies_browser or '',
                cookies_file=settings.cookies_file or ''
            )
            data = serialize_extract_result(result)
            data["is_playlist"] = is_playlist_or_channel_url(req.url)
            return {"ok": True, **data}
        except ExtractError as e:
            code_val = e.code.value if hasattr(e.code, 'value') else str(e.code)
            return {"ok": False, "code": code_val, "message": e.user_message, "is_playlist": is_playlist_or_channel_url(req.url)}
        except Exception as e:
            logging.error(f"Extract exception: {e}", exc_info=True)
            return {"ok": False, "code": "TEMPORARY_FAILURE", "message": "Something went wrong.", "is_playlist": is_playlist_or_channel_url(req.url)}

    @app.post("/api/playlist/extract")
    async def extract_playlist_endpoint(req: PlaylistExtractRequest):
        try:
            settings = app.state.settings
            result = await extract_playlist(
                req.url,
                cookies_browser=settings.cookies_browser or '',
                cookies_file=settings.cookies_file or '',
                max_items=req.max_items,
            )
            data = serialize_playlist_result(result)
            return {"ok": True, **data}
        except ExtractError as e:
            code_val = e.code.value if hasattr(e.code, 'value') else str(e.code)
            return {"ok": False, "code": code_val, "message": e.user_message}
        except Exception as e:
            logging.error(f"Playlist extract exception: {e}", exc_info=True)
            return {"ok": False, "code": "TEMPORARY_FAILURE", "message": "Could not extract playlist. Please try again."}

    @app.post("/api/playlist/download")
    async def start_playlist_download(req: PlaylistDownloadRequest):
        settings = app.state.settings
        dm: DownloadManager = app.state.download_manager
        try:
            items_dict = [item.dict() for item in req.items]
            task_ids = await dm.start_batch_download(
                items=items_dict,
                format_id=req.format_id,
                output_dir=settings.download_folder,
                cookies_browser=settings.cookies_browser or '',
                cookies_file=settings.cookies_file or '',
                format_label=req.format_label,
                playlist_id=req.playlist_id,
                playlist_title=req.playlist_title,
                subfolder=req.subfolder,
                numbered=req.numbered,
                sequential=req.sequential,
            )
            return {"ok": True, "download_ids": task_ids, "count": len(task_ids)}
        except Exception as e:
            logging.error(f"Playlist download exception: {e}", exc_info=True)
            return {"ok": False, "code": "TEMPORARY_FAILURE", "message": "Failed to start playlist download."}

    @app.post("/api/download")
    async def start_download(req: DownloadRequest):
        settings = app.state.settings
        dm: DownloadManager = app.state.download_manager
        try:
            download_id = await dm.start_download(
                req.video_url,
                req.format_id,
                req.title,
                settings.download_folder,
                cookies_browser=settings.cookies_browser or '',
                cookies_file=settings.cookies_file or '',
                format_label=req.format_label,
                thumbnail=req.thumbnail,
                platform=req.platform,
                quality=req.quality,
                duration=req.duration,
                direct_url=req.direct_url,
                needs_merge=req.needs_merge,
                has_audio=req.has_audio,
                total_bytes=req.total_bytes,
            )
            return {"ok": True, "download_id": download_id}
        except Exception as e:
            logging.error(f"Download exception: {e}", exc_info=True)
            return {"ok": False, "code": "TEMPORARY_FAILURE", "message": "Something went wrong."}

    @app.get("/api/downloads")
    async def list_downloads():
        dm: DownloadManager = app.state.download_manager
        tasks = dm.list_tasks()
        result = []
        for task in tasks:
            if hasattr(task, 'to_dict'):
                result.append(task.to_dict())
            else:
                result.append(task.__dict__.copy())
        return {"downloads": result}

    @app.get("/api/downloads/stream")
    async def stream_downloads(request: Request):
        dm: DownloadManager = app.state.download_manager

        async def event_generator():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    tasks = dm.list_tasks()
                    payload = [t.to_dict() if hasattr(t, "to_dict") else t.__dict__.copy() for t in tasks]
                    data_str = json.dumps({"downloads": payload}, ensure_ascii=False)
                    yield f"data: {data_str}\n\n"

                    has_active = any(
                        getattr(t, "status", "") in ("downloading", "merging", "preparing", "retrying")
                        for t in tasks
                    )
                    await asyncio.sleep(0.2 if has_active else 1.2)
            except asyncio.CancelledError:
                pass

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/download/cancel/{download_id}")
    async def cancel_download(download_id: str):
        dm: DownloadManager = app.state.download_manager
        success = await dm.cancel_download(download_id)
        return {"ok": success}

    @app.post("/api/download/pause/{download_id}")
    async def pause_download(download_id: str):
        dm: DownloadManager = app.state.download_manager
        success = await dm.pause_download(download_id)
        return {"ok": success}

    @app.post("/api/download/resume/{download_id}")
    async def resume_download(download_id: str):
        dm: DownloadManager = app.state.download_manager
        success = await dm.resume_download(download_id)
        return {"ok": success}

    @app.post("/api/download/retry/{download_id}")
    async def retry_download_endpoint(download_id: str):
        dm: DownloadManager = app.state.download_manager
        success = await dm.retry_download(download_id)
        return {"ok": success}

    @app.post("/api/download/remove/{download_id}")
    async def remove_download(download_id: str):
        dm: DownloadManager = app.state.download_manager
        success = dm.remove_task(download_id)
        return {"ok": success}

    @app.post("/api/play/{download_id}")
    async def play_download(download_id: str):
        dm: DownloadManager = app.state.download_manager
        task = dm.get_task(download_id)
        if not task or not task.file_path or not os.path.isfile(task.file_path):
            return {"ok": False, "message": "Media file not found on disk."}
        try:
            if sys.platform == "win32":
                os.startfile(task.file_path)
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", task.file_path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", task.file_path])
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    @app.post("/api/downloads/clear")
    async def clear_downloads(request: Request):
        dm: DownloadManager = app.state.download_manager
        only_completed = True
        try:
            params = request.query_params
            if "only_completed" in params:
                only_completed = params.get("only_completed", "true").lower() in ("true", "1")
        except Exception:
            pass
        dm.clear_completed(only_completed=only_completed)
        return {"ok": True}

    @app.post("/api/downloads/clear-incomplete")
    async def clear_incomplete_downloads(request: Request):
        dm: DownloadManager = app.state.download_manager
        include_pending = False
        try:
            params = request.query_params
            if "include_pending" in params:
                include_pending = params.get("include_pending", "false").lower() in ("true", "1")
        except Exception:
            pass
        count = dm.clear_incomplete(include_pending=include_pending)
        return {"ok": True, "count": count}

    @app.post("/api/downloads/pause-all")
    async def pause_all_downloads():
        dm: DownloadManager = app.state.download_manager
        count = await dm.pause_all()
        return {"ok": True, "count": count}

    @app.post("/api/downloads/resume-all")
    async def resume_all_downloads():
        dm: DownloadManager = app.state.download_manager
        count = await dm.resume_all()
        return {"ok": True, "count": count}

    @app.post("/api/queue/move")
    async def move_queue_task(req: QueueMoveRequest):
        dm: DownloadManager = app.state.download_manager
        success = dm.move_queued_task(req.download_id, req.direction)
        if not success:
            return JSONResponse(status_code=400, content={"ok": False, "message": "Task not in queue or invalid direction"})
        return {"ok": True}

    @app.post("/api/queue/reorder")
    async def reorder_queue_tasks(req: QueueReorderRequest):
        dm: DownloadManager = app.state.download_manager
        dm.reorder_queue(req.task_ids)
        return {"ok": True}

    @app.post("/api/reveal/path")
    async def reveal_file_path(req: Request):
        data = await req.json()
        file_path = (data.get("file_path") or "").strip()
        if not file_path:
            return {"ok": False, "message": "file_path is required"}
        download_dir = app.state.settings.download_folder

        if os.path.isdir(file_path):
            ok, message = await asyncio.to_thread(
                reveal_in_file_manager,
                "",
                file_path,
            )
            return {"ok": ok, "message": message}

        fallback_dir = download_dir
        if file_path:
            parent = os.path.dirname(file_path)
            if parent and os.path.isdir(parent) and "temp_downloads" not in parent.lower():
                fallback_dir = parent
                if not os.path.isfile(file_path):
                    found = _guess_output(parent, file_path)
                    if found:
                        file_path = found

        ok, message = await asyncio.to_thread(
            reveal_in_file_manager,
            file_path,
            fallback_dir,
        )
        return {"ok": ok, "message": message}

    @app.post("/api/reveal/{download_id}")
    async def reveal_download(download_id: str, req: Request):
        """Open the folder a finished download landed in, file selected.

        The path is looked up from the task or request body, falling back to
        the platform folder or the general download directory.
        """
        dm: DownloadManager = app.state.download_manager
        task = dm.get_task(download_id) if download_id != "current" else None

        file_path = ""
        try:
            body = await req.json()
            file_path = (body.get("file_path") or "").strip()
        except Exception:
            pass

        target_dir = ""
        if task is not None:
            if task.file_path and (not file_path or not os.path.exists(file_path)):
                file_path = task.file_path
            target_dir = getattr(task, "output_dir", "") or os.path.join(
                app.state.settings.download_folder, "Savitar", platform_folder_name(task.platform or task.video_url)
            )

        if not target_dir:
            if file_path:
                if os.path.isdir(file_path):
                    target_dir = file_path
                else:
                    parent = os.path.dirname(file_path)
                    if parent and os.path.isdir(parent) and "temp_downloads" not in parent.lower():
                        target_dir = parent
            if not target_dir:
                target_dir = app.state.settings.download_folder

        if file_path and "temp_downloads" in file_path.lower():
            file_path = ""

        if not os.path.isdir(target_dir):
            try:
                os.makedirs(target_dir, exist_ok=True)
            except Exception:
                target_dir = app.state.settings.download_folder

        if file_path and not os.path.isfile(file_path):
            if os.path.isdir(file_path):
                target_dir = file_path
                file_path = ""
            else:
                found = _guess_output(target_dir, file_path or (task.title if task else ""))
                if found:
                    file_path = found

        ok, message = await asyncio.to_thread(
            reveal_in_file_manager,
            file_path,
            target_dir,
        )
        return {"ok": ok, "message": message}

    @app.get("/api/dialog/file")
    async def dialog_pick_file(kind: str = "media"):
        def _pick():
            title = "Select Netscape cookies.txt File" if kind == "cookies" else "Select Media File to Convert"
            # Primary: Native Windows Win32 file dialog (zero external dependencies, reliable on all PCs)
            if sys.platform == "win32":
                try:
                    import ctypes
                    from ctypes import wintypes

                    class OPENFILENAMEW(ctypes.Structure):
                        _fields_ = [
                            ("lStructSize", wintypes.DWORD),
                            ("hwndOwner", wintypes.HWND),
                            ("hInstance", wintypes.HINSTANCE),
                            ("lpstrFilter", wintypes.LPCWSTR),
                            ("lpstrCustomFilter", wintypes.LPWSTR),
                            ("nMaxCustFilter", wintypes.DWORD),
                            ("nFilterIndex", wintypes.DWORD),
                            ("lpstrFile", wintypes.LPWSTR),
                            ("nMaxFile", wintypes.DWORD),
                            ("lpstrFileTitle", wintypes.LPWSTR),
                            ("nMaxFileTitle", wintypes.DWORD),
                            ("lpstrInitialDir", wintypes.LPCWSTR),
                            ("lpstrTitle", wintypes.LPCWSTR),
                            ("Flags", wintypes.DWORD),
                            ("nFileOffset", wintypes.WORD),
                            ("nFileExtension", wintypes.WORD),
                            ("lpstrDefExt", wintypes.LPCWSTR),
                            ("lCustData", wintypes.LPARAM),
                            ("lpfnHook", ctypes.c_void_p),
                            ("lpTemplateName", wintypes.LPCWSTR),
                            ("pvReserved", ctypes.c_void_p),
                            ("dwReserved", wintypes.DWORD),
                            ("FlagsEx", wintypes.DWORD),
                        ]

                    ofn = OPENFILENAMEW()
                    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
                    buf = ctypes.create_unicode_buffer(4096)
                    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
                    ofn.nMaxFile = 4096
                    ofn.lpstrTitle = title
                    if kind == "cookies":
                        ofn.lpstrFilter = "Cookie Files (*.txt)\0*.txt\0All Files (*.*)\0*.*\0\0"
                    else:
                        ofn.lpstrFilter = (
                            "Media Files\0*.mp4;*.mkv;*.webm;*.avi;*.mov;*.flv;*.ts;*.m4a;*.mp3;*.wav;*.aac;*.ogg;*.opus;*.flac\0"
                            "Video Files\0*.mp4;*.mkv;*.webm;*.avi;*.mov;*.flv;*.ts\0"
                            "Audio Files\0*.mp3;*.m4a;*.wav;*.aac;*.ogg;*.opus;*.flac\0"
                            "All Files (*.*)\0*.*\0\0"
                        )
                    ofn.Flags = 0x00080000 | 0x00001000 | 0x00000800

                    if ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
                        return buf.value
                    return ""
                except Exception as w_err:
                    logging.debug(f"Win32 file dialog error ({w_err}), falling back to Tkinter")

            # Secondary fallback: Tkinter
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                if kind == "cookies":
                    filetypes = [("Cookie Files", "*.txt"), ("All Files", "*.*")]
                else:
                    filetypes = [
                        ("Media Files", "*.mp4;*.mkv;*.webm;*.avi;*.mov;*.flv;*.ts;*.m4a;*.mp3;*.wav;*.aac;*.ogg;*.opus;*.flac"),
                        ("Video Files", "*.mp4;*.mkv;*.webm;*.avi;*.mov;*.flv;*.ts"),
                        ("Audio Files", "*.mp3;*.m4a;*.wav;*.aac;*.ogg;*.opus;*.flac"),
                        ("All Files", "*.*"),
                    ]
                path = filedialog.askopenfilename(title=title, filetypes=filetypes)
                root.destroy()
                return path or ""
            except Exception:
                return ""

        path = await asyncio.to_thread(_pick)
        return {"ok": bool(path), "path": path}

    @app.get("/api/dialog/folder")
    async def dialog_pick_folder():
        def _pick():
            title = "Select Download Folder"
            # Primary: Native Windows Win32 browse folder dialog
            if sys.platform == "win32":
                try:
                    import ctypes
                    from ctypes import wintypes

                    class BROWSEINFOW(ctypes.Structure):
                        _fields_ = [
                            ("hwndOwner", wintypes.HWND),
                            ("pidlRoot", ctypes.c_void_p),
                            ("pszDisplayName", wintypes.LPWSTR),
                            ("lpszTitle", wintypes.LPCWSTR),
                            ("ulFlags", wintypes.UINT),
                            ("lpfn", ctypes.c_void_p),
                            ("lParam", wintypes.LPARAM),
                            ("iImage", ctypes.c_int),
                        ]

                    bi = BROWSEINFOW()
                    display_name = ctypes.create_unicode_buffer(260)
                    bi.pszDisplayName = ctypes.cast(display_name, wintypes.LPWSTR)
                    bi.lpszTitle = title
                    bi.ulFlags = 0x00000001 | 0x00000040  # BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE

                    pidl = ctypes.windll.shell32.SHBrowseForFolderW(ctypes.byref(bi))
                    if pidl:
                        path_buf = ctypes.create_unicode_buffer(260)
                        ctypes.windll.shell32.SHGetPathFromIDListW(pidl, path_buf)
                        ctypes.windll.ole32.CoTaskMemFree(pidl)
                        return path_buf.value
                    return ""
                except Exception as w_err:
                    logging.debug(f"Win32 folder dialog error ({w_err}), falling back to Tkinter")

            # Secondary fallback: Tkinter
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                path = filedialog.askdirectory(title=title)
                root.destroy()
                return path or ""
            except Exception:
                return ""

        path = await asyncio.to_thread(_pick)
        return {"ok": bool(path), "path": path}

    @app.post("/api/convert")
    async def convert_endpoint(req: ConvertRequest):
        from core.converter import convert_file
        res = await convert_file(
            source_path=req.source_path,
            target_format=req.target_format,
            output_dir=req.output_dir or None,
        )
        return res

    @app.get("/api/settings")
    async def get_settings():
        d = app.state.settings.to_dict()
        try:
            from core.autostart import get_autostart_info
            info = get_autostart_info()
            d["start_with_windows"] = info["enabled"]
            d["autostart_registered"] = info["registered"]
            d["autostart_task_manager_disabled"] = info["task_manager_disabled"]
        except Exception:
            pass
        return d

    @app.post("/api/settings")
    async def update_settings(req: Request):
        data = await req.json()
        settings: AppSettings = app.state.settings
        _STR_FIELDS = {"download_folder", "cookies_browser", "cookies_file", "last_ytdlp_version", "preferred_quality"}
        _INT_FIELDS = {"window_width", "window_height"}
        _BOOL_FIELDS = {"quick_download_shortcut", "start_with_windows"}
        for k, v in data.items():
            if k in _STR_FIELDS:
                if isinstance(v, str):
                    setattr(settings, k, v)
            elif k in _INT_FIELDS:
                if isinstance(v, (int, float)) and v > 0:
                    setattr(settings, k, int(v))
            elif k in _BOOL_FIELDS:
                setattr(settings, k, bool(v))
        if "start_with_windows" in data:
            try:
                from core.autostart import set_autostart
                val = bool(data["start_with_windows"])
                settings.start_with_windows = val
                set_autostart(val)
            except Exception as exc:
                logging.warning(f"Error updating autostart: {exc}")
        if "speed_limit_kbps" in data:
            try:
                sl = int(data["speed_limit_kbps"])
                if sl >= 0:
                    settings.speed_limit_kbps = sl
                    dm = getattr(app.state, "download_manager", None)
                    if dm and hasattr(dm, "rate_limiter"):
                        dm.rate_limiter.set_speed_limit(sl)
            except (ValueError, TypeError):
                pass
        if "max_concurrent_downloads" in data:
            try:
                mcd = int(data["max_concurrent_downloads"])
                if 1 <= mcd <= 10:
                    settings.max_concurrent_downloads = mcd
            except (ValueError, TypeError):
                pass
        settings.save()
        tray_icon = getattr(app.state, "tray_icon", None)
        if tray_icon and hasattr(tray_icon, "set_quick_download_enabled"):
            tray_icon.set_quick_download_enabled(settings.quick_download_shortcut)
        return {"ok": True}

    @app.get("/api/system/disk-space")
    async def get_disk_space():
        import shutil
        download_folder = app.state.settings.download_folder

        def _check():
            target = download_folder
            if not os.path.exists(target):
                parent = os.path.dirname(target)
                target = parent if parent and os.path.exists(parent) else os.path.expanduser("~")
            try:
                usage = shutil.disk_usage(target)
                return {
                    "ok": True,
                    "path": download_folder,
                    "free_bytes": usage.free,
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                    "free_gb": round(usage.free / (1024**3), 2),
                    "total_gb": round(usage.total / (1024**3), 2),
                }
            except Exception as e:
                return {"ok": False, "error": str(e), "free_bytes": 0}

        return await asyncio.to_thread(_check)

    @app.get("/api/browsers")
    async def get_browsers():
        return {"browsers": detect_installed_browsers()}

    # ── Engine (yt-dlp / aria2c / ffmpeg) ─────────────────────────
    # ``/api/engines`` is the current endpoint; the two below it are kept
    # because older UI builds still call them.

    @app.get("/api/engines")
    async def get_engines():
        return {"ok": True, **await engine_status()}

    @app.post("/api/engines/update")
    async def update_engines(tool: str = ""):
        """Update one tool (``?tool=aria2c``) or all three when omitted."""
        single = {
            "ytdlp": update_ytdlp,
            "yt-dlp": update_ytdlp,
            "aria2c": update_aria2c,
            "ffmpeg": update_ffmpeg,
        }.get(tool.lower().strip())

        if single:
            res = await single()
            return {"ok": res.get("ok", False), "message": res.get("message", ""),
                    "updated_tool": tool, "status": await engine_status()}

        return await update_all()

    # ── Multi-Gateway Health & Updates ───────────────────────────

    @app.get("/api/gateways/health")
    async def get_gateways_health():
        from core.gateways.health import health_tracker
        return health_tracker.get_health_status()

    @app.post("/api/gateways/tiktok/update")
    async def update_tiktok_gateway(force: bool = False):
        from core.gateways.tiktok_updater import tiktok_updater
        return await tiktok_updater.stage_and_promote_update(force=force)

    @app.post("/api/gateways/tiktok/rollback")
    async def rollback_tiktok_gateway():
        from core.gateways.tiktok_updater import tiktok_updater
        return await tiktok_updater.rollback()

    @app.get("/api/clipboard")
    async def get_clipboard_text_endpoint():
        from core.clipboard import get_clipboard_text
        text = await asyncio.to_thread(get_clipboard_text)
        return {"ok": True, "text": text}


    # ── Application Auto-Update (GitHub Releases) ────────────────
    @app.get("/api/app/version")
    async def get_app_version():
        return {
            "ok": True,
            "version": APP_VERSION,
            "repo": getattr(app.state.settings, "github_repo", "kamstackbuild/Savitar"),
        }

    @app.get("/api/app/check-update")
    async def check_for_app_updates(repo: str = ""):
        target_repo = repo or getattr(app.state.settings, "github_repo", "kamstackbuild/Savitar")
        return await check_app_update(repo=target_repo, current_ver=APP_VERSION)

    @app.post("/api/app/open-url")
    async def open_external_url(req: Request):
        try:
            body = await req.json()
            url = (body.get("url") or "").strip()
            if url and (url.startswith("http://") or url.startswith("https://")):
                import webbrowser
                await asyncio.to_thread(webbrowser.open, url)
                return {"ok": True, "opened": url}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": "Invalid URL"}

    return app

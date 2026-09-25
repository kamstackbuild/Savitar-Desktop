import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

def _safe_home() -> Path:
    try:
        return Path.home()
    except Exception:
        import tempfile
        fallback = os.environ.get("USERPROFILE") or os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or tempfile.gettempdir()
        return Path(fallback)

def _default_download_folder() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as k:
                val, _ = winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")
                folder = os.path.expandvars(str(val))
                if folder and os.path.isdir(folder):
                    return folder
        except Exception:
            pass
    dl = _safe_home() / 'Downloads'
    try:
        dl.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return str(dl)

def _settings_dir() -> Path:
    from core.binaries import app_dir
    for marker in ("portable", "portable.dat"):
        if (app_dir() / marker).is_file():
            d = app_dir() / "data"
            d.mkdir(parents=True, exist_ok=True)
            return d
    local = os.environ.get('LOCALAPPDATA')
    if local:
        p = Path(local) / 'Savitar'
        legacy = os.environ.get('APPDATA')
        if legacy and (Path(legacy) / 'Savitar' / 'settings.json').is_file() and not (p / 'settings.json').is_file():
            return Path(legacy) / 'Savitar'
        return p
    return Path(os.environ.get('APPDATA', _safe_home())) / 'Savitar'

def _settings_path() -> Path:
    return _settings_dir() / 'settings.json'

@dataclass
class AppSettings:
    download_folder: str = field(default_factory=_default_download_folder)
    cookies_browser: str = ''
    cookies_file: str = ''
    last_ytdlp_version: str = ''
    window_width: int = 900
    window_height: int = 680
    github_repo: str = 'kamstackbuild/Savitar'
    app_version: str = '1.0.0'
    quick_download_shortcut: bool = True
    preferred_quality: str = 'max'  # 'max' | 'best' | '1080p' | '720p' | '480p' | '360p' | 'audio'
    speed_limit_kbps: int = 0  # 0 = unlimited, > 0 = maximum speed in KB/s
    max_concurrent_downloads: int = 5  # 1 to 10 simultaneous downloads
    start_with_windows: bool = True

    @staticmethod
    def load() -> 'AppSettings':
        path = _settings_path()
        settings = AppSettings()
        has_file = path.exists()
        if has_file:
            try:
                data = json.loads(path.read_text('utf-8'))
                settings = AppSettings(**{k: v for k, v in data.items() if k in AppSettings.__dataclass_fields__})
            except Exception:
                pass
        else:
            # First launch: ensure defaults and sync with registry
            settings.start_with_windows = True
            settings.quick_download_shortcut = True
            settings.preferred_quality = 'max'
            try:
                from core.autostart import set_autostart
                set_autostart(True)
            except Exception:
                pass

        # Sync registry if start_with_windows is enabled
        try:
            from core.autostart import is_run_key_present, set_autostart
            if settings.start_with_windows and not is_run_key_present():
                set_autostart(True)
            elif not settings.start_with_windows and is_run_key_present():
                set_autostart(False)
        except Exception:
            pass
        return settings

    def save(self) -> None:
        d = _settings_dir()
        d.mkdir(parents=True, exist_ok=True)
        _settings_path().write_text(json.dumps(asdict(self), indent=2), 'utf-8')

    def to_dict(self) -> dict:
        return asdict(self)

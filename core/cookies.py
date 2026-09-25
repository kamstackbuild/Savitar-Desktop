import asyncio
import os
import sys

from .binaries import popen_kwargs, ytdlp_argv

BROWSERS = [
    {'id': 'chrome', 'name': 'Google Chrome'},
    {'id': 'firefox', 'name': 'Mozilla Firefox'},
    {'id': 'edge', 'name': 'Microsoft Edge'},
    {'id': 'brave', 'name': 'Brave'},
    {'id': 'opera', 'name': 'Opera'},
    {'id': 'vivaldi', 'name': 'Vivaldi'},
]

def detect_installed_browsers() -> list[dict]:
    browsers = []

    if sys.platform != 'win32':
        for b in BROWSERS:
            browsers.append({'id': b['id'], 'name': b['name'], 'available': True})
        return browsers

    localappdata = os.environ.get('LOCALAPPDATA', '')
    appdata = os.environ.get('APPDATA', '')

    paths = {
        'chrome': os.path.join(localappdata, 'Google', 'Chrome', 'User Data'),
        'firefox': os.path.join(appdata, 'Mozilla', 'Firefox', 'Profiles'),
        'edge': os.path.join(localappdata, 'Microsoft', 'Edge', 'User Data'),
        'brave': os.path.join(localappdata, 'BraveSoftware', 'Brave-Browser', 'User Data'),
        'opera': os.path.join(appdata, 'Opera Software', 'Opera Stable'),
        'vivaldi': os.path.join(localappdata, 'Vivaldi', 'User Data'),
    }

    for b in BROWSERS:
        path = paths.get(b['id'])
        available = path is not None and os.path.exists(path)
        browsers.append({'id': b['id'], 'name': b['name'], 'available': available})

    return browsers

async def test_browser_cookies(browser_id: str) -> dict:
    test_url = "https://www.tiktok.com/@tiktok/video/7106594312292453675"

    argv = [
        *ytdlp_argv(),
        "--dump-single-json", "--no-warnings",
        "--cookies-from-browser", browser_id,
        "--", test_url,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **popen_kwargs(),
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=30.0)

        if proc.returncode == 0:
            return {"ok": True, "message": "Cookies accessed successfully"}
        stderr = (stderr_b or b"").decode("utf-8", "replace")
        return {"ok": False, "message": f"Failed to access cookies: {stderr.strip()}"}
    except Exception as e:
        return {"ok": False, "message": str(e)}

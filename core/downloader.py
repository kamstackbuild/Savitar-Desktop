"""Savitar downloader.

yt-dlp drives the download and aria2c is plugged in as its external downloader
(``--downloader aria2c``). That is deliberately different from resolving URLs
ourselves and shelling out to aria2c directly: yt-dlp keeps handling
per-format headers, HLS/DASH fragments, merging and post-processing, while
aria2c supplies the many-connection speed. Progress is read from yt-dlp's
``--newline`` output, which stays accurate for both engines.

Notes on things that previously made this slow or silent:
  * aria2c writes its progress to the *console readout*; with
    ``--show-console-readout=false`` there is nothing to parse, so speed and
    percentage sat at zero. yt-dlp's own progress line is used instead.
  * aria2c cannot report progress for HLS/DASH fragment downloads, so those are
    handled by yt-dlp with concurrent fragments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from urllib.parse import urlparse

import httpx

from .binaries import app_dir, aria2c_path, data_dir, ffmpeg_dir, js_runtime_argv, popen_kwargs, ytdlp_argv
from .platforms import cdn_referer_for_host, find_platform, platform_folder_name
from .segmented import SegmentedDownloader
from .updater import FFMPEG_WAIT_S, ensure_ffmpeg

logger = logging.getLogger(__name__)

# aria2c connection tuning. 16 connections per server is the practical ceiling
# most CDNs allow before they start throttling or resetting.
ARIA2_CONNECTIONS = 16
ARIA2_SPLIT = 16
ARIA2_MIN_SPLIT = "1M"
CONCURRENT_FRAGMENTS = 16

# Task-level retry: when yt-dlp exits non-zero due to a transient network
# error, retry the whole download up to MAX_TASK_RETRIES times with
# exponential backoff (RETRY_BASE_DELAY * 2^attempt seconds).
#
# Kept deliberately low: _run() already walks a multi-step fallback cascade
# (aria2c -> native -> direct stream -> re-extract) internally, so every extra
# task-level attempt multiplies that whole cascade. With the old value of 3 a
# dead link could take tens of minutes to report a failure.
MAX_TASK_RETRIES = 3
RETRY_BASE_DELAY = 2          # seconds before first retry (2s, 4s, 8s exponential backoff)
RETRY_MAX_DELAY = 10          # cap on the exponential backoff

# Stall watchdog. yt-dlp/aria2c print progress continuously, so a long silence
# means the transfer is wedged. Without this the reader below awaits forever,
# permanently occupying one of MAX_CONCURRENT_DOWNLOADS slots and leaving every
# queued download displaying "Pending".
STALL_TIMEOUT_S = 120         # no output at all while downloading
STARTUP_STALL_TIMEOUT_S = 180  # before the first byte: extraction is quiet
MERGE_STALL_TIMEOUT_S = 900   # ffmpeg merge/extract runs quietly for a while
STALL_CHECK_INTERVAL_S = 5

# How long a retrying task may wait for a concurrency slot before it is handed
# back to the queue instead of holding this coroutine open.
SLOT_WAIT_TIMEOUT_S = 60

# Statuses that mean "this task is working" — used for the queue's
# sequential-batch check and mirrored by the UI.
ACTIVE_STATUSES = ("preparing", "downloading", "merging", "retrying")


def _bytes_from_value(value: str, unit: str) -> int:
    multipliers = {
        "B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
        "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4,
        "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
        "k": 1024, "m": 1024**2, "g": 1024**3,
        "kb": 1000, "mb": 1000**2, "gb": 1000**3,
        "kib": 1024, "mib": 1024**2, "gib": 1024**3,
    }
    try:
        val = str(value).lstrip("~").strip()
        u = str(unit).strip()
        return int(float(val) * multipliers.get(u, 1))
    except (ValueError, TypeError):
        return 0


def _seconds_from_eta(value: str) -> int:
    parts = value.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, IndexError):
        pass
    return 0


MAX_CONCURRENT_DOWNLOADS = 5


def sanitize_folder_name(name: str) -> str:
    """Strip illegal filesystem characters and limit length for Windows folder names."""
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip().rstrip(". ")
    return cleaned[:60] or "YouTube Playlist"


def _safe_kill_process(proc) -> None:
    """Safely terminate a subprocess and close its open Windows pipe transports."""
    if proc is None:
        return
    try:
        proc.kill()
    except Exception:
        pass
    try:
        transport = getattr(proc, "_transport", None)
        if transport and hasattr(transport, "close"):
            transport.close()
    except Exception:
        pass


def _clean_quality_tag(task: "DownloadTask") -> str:
    """Extract a clean quality badge for the filename (e.g. '1080p', '720p', '4K', 'MP3')
    replacing noisy video IDs like [dQw4w9WgXcQ]."""
    q = str(getattr(task, "quality", "") or "").strip()
    if q:
        m = re.search(r'\b(8K|4K|2K|2160p|1440p|1080p|720p|480p|360p|240p|144p|MP3|M4A)\b', q, re.IGNORECASE)
        if m:
            val = m.group(1)
            return val.lower() if val.lower().endswith("p") else val.upper()
    lbl = str(getattr(task, "format_label", "") or "").strip()
    if lbl:
        m = re.search(r'\b(8K|4K|2K|2160p|1440p|1080p|720p|480p|360p|240p|144p|MP3|M4A)\b', lbl, re.IGNORECASE)
        if m:
            val = m.group(1)
            return val.lower() if val.lower().endswith("p") else val.upper()
    fid = str(getattr(task, "format_id", "") or "").strip()
    if "mp3" in fid.lower():
        return "MP3"
    m = re.search(r'height<=(\d+)', fid)
    if m:
        return f"{m.group(1)}p"
    if q and len(q) <= 12:
        return re.sub(r'[\\/*?:"<>|]', "", q).strip()
    return ""


def _is_manifest_url(url: str) -> bool:
    """True when URL points to an HLS or DASH manifest rather than a direct media file."""
    u = (url or "").lower()
    return ".m3u8" in u or ".mpd" in u or "manifest" in u



@dataclass
class DownloadTask:
    id: str
    video_url: str
    format_id: str
    title: str
    file_path: str = ""
    # pending    → queued, waiting for a concurrency slot
    # preparing  → slot taken, fetching a prerequisite (ffmpeg) before starting
    # downloading/merging → yt-dlp is working
    # retrying   → transient failure, waiting out the backoff (slot released)
    # completed|failed|cancelled|paused → terminal / user controlled
    status: str = "pending"
    progress: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    expected_bytes: int = 0
    speed_bps: float = 0.0
    eta_seconds: int = 0
    error: str = ""
    # Rich metadata for UI cards
    format_label: str = ""
    thumbnail: str = ""
    platform: str = ""
    quality: str = ""
    duration: int = 0
    engine: str = "ytdlp"     # "aria2c" | "ytdlp" | "direct"
    direct_url: str = ""      # Direct fallback media stream URL
    # True when the selection merges separate video+audio streams. Falling
    # back to ``direct_url`` for such a task would save a silent video-only
    # stream, so the direct path is only allowed for self-contained media.
    needs_merge: bool = False
    has_audio: bool = True
    # 0 = original download, 1 = second time this video is saved here (" 1"
    # appended to the name), and so on. Keeps repeat downloads from colliding
    # with the file already on disk (yt-dlp would otherwise skip them).
    copy_index: int = 0
    # Playlist / batch metadata
    playlist_id: str = ""
    playlist_title: str = ""
    batch_id: str = ""
    index: int = 0
    output_dir: str = ""
    queue_position: int = 0

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        d["progress"] = round(self.progress, 1)
        return d


class TokenBucketRateLimiter:
    """Cooperative global token-bucket rate limiter for downloads.

    Operates globally across all active downloads and worker coroutines.
    speed_limit_kbps: 0 = unlimited, > 0 = maximum aggregate speed in KB/s.
    """

    def __init__(self, speed_limit_kbps: int = 0):
        self.speed_limit_kbps = max(0, speed_limit_kbps)
        self._tokens: float = float(self.rate_bytes_per_sec)
        self._last_time: float = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate_bytes_per_sec(self) -> int:
        return self.speed_limit_kbps * 1024

    def set_speed_limit(self, speed_limit_kbps: int) -> None:
        self.speed_limit_kbps = max(0, speed_limit_kbps)
        rate = float(self.rate_bytes_per_sec)
        if rate > 0:
            self._tokens = min(self._tokens, rate)

    async def acquire(self, num_bytes: int) -> None:
        if self.speed_limit_kbps <= 0 or num_bytes <= 0:
            return

        rate = float(self.rate_bytes_per_sec)
        max_capacity = max(rate, float(num_bytes))

        while True:
            sleep_time = 0.0
            async with self._lock:
                if self.speed_limit_kbps <= 0:
                    return
                now = time.monotonic()
                elapsed = now - self._last_time
                self._last_time = now

                self._tokens = min(max_capacity, self._tokens + (elapsed * rate))

                if self._tokens >= num_bytes:
                    self._tokens -= num_bytes
                    return

                needed = num_bytes - self._tokens
                sleep_time = max(0.001, needed / rate)

            await asyncio.sleep(min(sleep_time, 0.25))


class DownloadManager:
    def __init__(self):
        self._tasks: dict[str, DownloadTask] = {}
        self._queue: deque[tuple[DownloadTask, str, str, str, str]] = deque()
        self._running_task_ids: set[str] = set()
        self._processes: dict[str, list[asyncio.subprocess.Process]] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._sequential_batches: set[str] = set()  # batch_ids that download 1-by-1
        # Next copy number per "{folder}|{title prefix}", so downloading the
        # same link again saves "Title 1", "Title 2", … instead of yt-dlp
        # skipping with "has already been downloaded".
        self._copy_counters: dict[str, int] = {}
        self.rate_limiter = TokenBucketRateLimiter()
        self.on_task_completed: Optional[Callable[[DownloadTask], None]] = None
        self._load_history()
        self._cleanup_abandoned_temp_files()
        self._update_queue_positions()

    @property
    def max_concurrent_downloads(self) -> int:
        try:
            from core.settings import AppSettings
            return max(1, min(10, getattr(AppSettings.load(), "max_concurrent_downloads", MAX_CONCURRENT_DOWNLOADS)))
        except Exception:
            return MAX_CONCURRENT_DOWNLOADS

    def _update_queue_positions(self) -> None:
        """Update 1-based queue_position for all tasks in _queue, and clear for others."""
        queued_ids = set()
        for idx, entry in enumerate(self._queue, 1):
            task = entry[0]
            task.queue_position = idx
            queued_ids.add(task.id)
        for tid, task in self._tasks.items():
            if tid not in queued_ids and getattr(task, "queue_position", 0) != 0:
                task.queue_position = 0

    def _cleanup_abandoned_temp_files(self) -> None:
        """Clean up incomplete temporary download files (.part, .ytdl, etc.) older than 48 hours."""
        try:
            temp_dir = os.path.join(str(data_dir()), "temp_downloads")
            if not os.path.isdir(temp_dir):
                return
            now = time.time()
            cutoff = now - (48 * 3600)  # 48 hours
            active_paths = {t.file_path for t in self._tasks.values() if t.status in ACTIVE_STATUSES and t.file_path}
            for entry in os.scandir(temp_dir):
                if not entry.is_file():
                    continue
                name = entry.name.lower()
                if any(name.endswith(ext) for ext in (".part", ".ytdl", ".temp", ".aria2", ".meta.json")):
                    full_path = entry.path
                    if any(act in full_path for act in active_paths if act):
                        continue
                    try:
                        if entry.stat().st_mtime < cutoff:
                            os.remove(full_path)
                            logger.info(f"Cleaned up orphaned temporary file: {entry.name}")
                    except OSError:
                        pass
        except Exception as exc:
            logger.debug(f"Temp files cleanup error: {exc}")

    # ── History persistence ───────────────────────────────────────

    @staticmethod
    def _history_path() -> str:
        primary = os.path.join(str(data_dir()), "downloads_history.json")
        if not os.path.isfile(primary):
            legacy_appdata = os.environ.get("APPDATA")
            if legacy_appdata:
                legacy = os.path.join(legacy_appdata, "Savitar", "downloads_history.json")
                if os.path.isfile(legacy):
                    return legacy
        return primary

    def _load_history(self) -> None:
        path = self._history_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return
            for item in data:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                status = item.get("status", "completed")
                if status in ("downloading", "merging", "pending", "preparing", "retrying"):
                    status = "cancelled"
                    item["error"] = item.get("error") or "Download stopped when application was closed"
                    item["speed_bps"] = 0.0
                    item["eta_seconds"] = 0

                task_kwargs = {
                    k: v for k, v in item.items()
                    if k in DownloadTask.__dataclass_fields__
                }
                task_kwargs["status"] = status
                task = DownloadTask(**task_kwargs)
                self._tasks[task.id] = task
        except Exception as exc:
            logger.warning(f"Could not load download history: {exc}")

    def _save_history(self) -> None:
        try:
            path = self._history_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            save_list = []
            for t in list(self._tasks.values()):
                d = t.to_dict()
                save_list.append(d)
            if len(save_list) > 500:
                save_list = save_list[-500:]
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(save_list, f, indent=2, ensure_ascii=False)
            if os.path.exists(path):
                try:
                    os.replace(tmp_path, path)
                except OSError:
                    os.remove(path)
                    os.replace(tmp_path, path)
            else:
                os.rename(tmp_path, path)
        except Exception as exc:
            logger.warning(f"Could not save download history: {exc}")

    # ── Repeat-download naming ────────────────────────────────────

    @staticmethod
    def _title_prefix(title: str) -> str:
        """Approximate yt-dlp's sanitized title for on-disk matching."""
        cleaned = re.sub(r'[\\/*?:"<>|]', "", title or "").strip().lower()
        return re.sub(r"\s+", " ", cleaned)[:120]

    def _next_copy_index(self, target_dir: str, title: str) -> int:
        """Return 0 for the first download of this title in ``target_dir``,
        1 for the second, and so on.

        The count starts from files already on disk (so numbers survive app
        restarts) and is continued in memory while the app runs.
        """
        prefix = self._title_prefix(title)
        if not prefix:
            return 0
        key = f"{os.path.normcase(target_dir)}|{prefix}"
        if key not in self._copy_counters:
            count = 0
            try:
                if os.path.isdir(target_dir):
                    for name in os.listdir(target_dir):
                        stem = os.path.splitext(name)[0].strip().lower()
                        if stem.startswith(prefix):
                            count += 1
            except OSError:
                count = 0
            self._copy_counters[key] = count
        self._copy_counters[key] += 1
        return self._copy_counters[key] - 1

    def _release_copy_index(self, task: DownloadTask) -> None:
        """Give back a copy number claimed by a cancelled/removed download.

        Only released when nothing landed on disk — otherwise the next
        download would reuse the number and collide with the existing file.
        """
        if getattr(task, "copy_index", 0) > 0:
            if task.file_path and os.path.isfile(task.file_path):
                return
            prefix = self._title_prefix(task.title)
            key = f"{os.path.normcase(task.output_dir)}|{prefix}"
            if key in self._copy_counters and self._copy_counters[key] > 0:
                self._copy_counters[key] -= 1
            task.copy_index = 0

    # ── Public API ────────────────────────────────────────────────

    async def start_download(
        self,
        video_url: str,
        format_id: str,
        title: str,
        output_dir: str,
        cookies_browser: str = "",
        cookies_file: str = "",
        format_label: str = "",
        thumbnail: str = "",
        platform: str = "",
        quality: str = "",
        duration: int = 0,
        playlist_id: str = "",
        playlist_title: str = "",
        batch_id: str = "",
        index: int = 0,
        direct_url: str = "",
        needs_merge: bool = False,
        has_audio: bool = True,
        total_bytes: int = 0,
    ) -> str:
        task_id = str(uuid.uuid4())
        cdn_referer = cdn_referer_for_host(video_url)

        # Organize downloads by: {Base Downloads Folder} / Savitar / {Platform}
        base_dir = output_dir or os.path.join(os.path.expanduser("~"), "Downloads")
        plat_folder = platform_folder_name(platform or video_url)
        target_dir = os.path.join(base_dir, "Savitar", plat_folder)
        os.makedirs(target_dir, exist_ok=True)

        task = DownloadTask(
            id=task_id,
            video_url=video_url,
            format_id=format_id,
            title=title,
            output_dir=target_dir,
            format_label=format_label,
            thumbnail=thumbnail,
            platform=platform,
            quality=quality,
            duration=duration,
            total_bytes=total_bytes,
            expected_bytes=total_bytes,
            engine="aria2c" if aria2c_path() else "ytdlp",
            direct_url=direct_url if (has_audio and not needs_merge) else "",
            needs_merge=needs_merge,
            has_audio=has_audio,
            copy_index=self._next_copy_index(target_dir, title),
            playlist_id=playlist_id,
            playlist_title=playlist_title,
            batch_id=batch_id,
            index=index,
            status="pending",
        )
        task._run_params = (target_dir, cookies_browser, cookies_file, cdn_referer)
        self._tasks[task_id] = task
        self._save_history()
        self._queue.append((task, target_dir, cookies_browser, cookies_file, cdn_referer))
        self._update_queue_positions()
        asyncio.create_task(self._pump_queue())
        return task_id

    async def start_batch_download(
        self,
        items: list[dict],
        format_id: str,
        output_dir: str,
        cookies_browser: str = "",
        cookies_file: str = "",
        format_label: str = "",
        playlist_id: str = "",
        playlist_title: str = "",
        subfolder: bool = True,
        numbered: bool = True,
        sequential: bool = False,
    ) -> list[str]:
        """Enqueue multiple playlist/channel videos into the concurrency-managed download queue."""
        batch_id = str(uuid.uuid4())
        base_output_dir = output_dir or os.path.join(os.path.expanduser("~"), "Downloads")

        if sequential:
            self._sequential_batches.add(batch_id)

        first_url = items[0].get("video_url") or items[0].get("url") if items else ""
        first_plat = items[0].get("platform") if items else ""
        plat_folder = platform_folder_name(first_plat or first_url or "YouTube")
        savitar_plat_dir = os.path.join(base_output_dir, "Savitar", plat_folder)

        target_dir = savitar_plat_dir
        if subfolder and playlist_title:
            folder_name = sanitize_folder_name(playlist_title)
            target_dir = os.path.join(savitar_plat_dir, folder_name)
        os.makedirs(target_dir, exist_ok=True)

        task_ids: list[str] = []
        for i, item in enumerate(items, start=1):
            vid_url = item.get("video_url") or item.get("url") or ""
            if not vid_url:
                continue
            item_title = item.get("title") or f"Video {i}"
            item_thumb = item.get("thumbnail") or ""
            item_dur = int(item.get("duration") or 0)
            item_idx = item.get("index") or (i if numbered else 0)

            task_id = str(uuid.uuid4())
            cdn_referer = cdn_referer_for_host(vid_url)
            task = DownloadTask(
                id=task_id,
                video_url=vid_url,
                format_id=format_id,
                title=item_title,
                output_dir=target_dir,
                format_label=format_label,
                thumbnail=item_thumb,
                platform=first_plat or item.get("platform") or "",
                duration=item_dur,
                engine="aria2c" if aria2c_path() else "ytdlp",
                playlist_id=playlist_id,
                playlist_title=playlist_title,
                batch_id=batch_id,
                index=item_idx if numbered else 0,
                status="pending",
            )
            task._run_params = (target_dir, cookies_browser, cookies_file, cdn_referer)
            self._tasks[task_id] = task
            self._queue.append((task, target_dir, cookies_browser, cookies_file, cdn_referer))
            task_ids.append(task_id)

        self._save_history()
        self._update_queue_positions()
        asyncio.create_task(self._pump_queue())
        return task_ids

    async def _pump_queue(self):
        """Dispatch queued downloads while staying within MAX_CONCURRENT_DOWNLOADS.

        For sequential batches (user chose "One by one"), only one task from
        that batch is allowed to run at a time.
        """
        async with self._lock:
            deferred: list[tuple[DownloadTask, str, str, str, str]] = []
            while len(self._running_task_ids) < self.max_concurrent_downloads and self._queue:
                entry = self._queue.popleft()
                task = entry[0]

                # If task was cancelled, removed, or no longer pending, skip it completely!
                if task.status != "pending" or task.id not in self._tasks:
                    continue

                # Sequential batch: strictly 1 active task per batch at a time
                if task.batch_id and task.batch_id in self._sequential_batches:
                    batch_running = any(
                        (t.batch_id == task.batch_id and t.id != task.id)
                        for tid in list(self._running_task_ids)
                        if (t := self._tasks.get(tid)) and t.status not in ("completed", "failed", "cancelled")
                    )
                    if batch_running:
                        deferred.append(entry)
                        continue

                # Mark preparing immediately to prevent subsequent loop iterations from dispatching
                # concurrent downloads from the same sequential batch.
                task.status = "preparing"
                task.queue_position = 0
                self._running_task_ids.add(task.id)
                asyncio.create_task(
                    self._run_task_wrapper(task, entry[1], entry[2], entry[3], entry[4])
                )

            # Put deferred items back at the front of the queue preserving FIFO order
            for item in reversed(deferred):
                self._queue.appendleft(item)

            self._update_queue_positions()

            # Housekeep finished sequential batches
            if self._sequential_batches:
                active_batches = {
                    t.batch_id for t in self._tasks.values()
                    if t.batch_id and t.status in ("pending", "preparing", "downloading", "merging", "retrying", "paused")
                }
                self._sequential_batches.intersection_update(active_batches)

    async def _acquire_slot(self, task: DownloadTask, timeout: float) -> bool:
        """Wait for a free concurrency slot, polling until ``timeout``.

        Used when a retrying task comes back from its backoff sleep. Returns
        False if the task went away (cancelled/paused/removed) or no slot
        opened in time — the caller then re-queues it.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if task.id not in self._tasks or task.status in ("cancelled", "paused"):
                return False
            async with self._lock:
                if len(self._running_task_ids) < self.max_concurrent_downloads:
                    self._running_task_ids.add(task.id)
                    return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.25)

    async def _run_task_wrapper(
        self,
        task: DownloadTask,
        output_dir: str,
        cookies_browser: str,
        cookies_file: str,
        cdn_referer: str = "",
    ):
        holds_slot = True
        try:
            for attempt in range(MAX_TASK_RETRIES + 1):
                if task.status in ("cancelled", "paused") or task.id not in self._tasks:
                    break

                if not holds_slot:
                    # Coming back from a backoff sleep — take a slot again.
                    if not await self._acquire_slot(task, SLOT_WAIT_TIMEOUT_S):
                        if task.id in self._tasks and task.status == "retrying":
                            task.status = "pending"
                            task.error = ""
                            self._queue.append(
                                (task, output_dir, cookies_browser, cookies_file, cdn_referer)
                            )
                            self._update_queue_positions()
                        return
                    holds_slot = True
                task.status = "preparing"
                self._save_history()
                await self._run(task, output_dir, cookies_browser, cookies_file, cdn_referer)

                if task.status != "failed" or attempt >= MAX_TASK_RETRIES:
                    break                          # success, cancelled, or out of attempts
                if self._is_fatal(task.error) or not self._is_retryable(task.error):
                    break                          # nothing a retry would change

                delay = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** attempt))
                task.status = "retrying"
                task.error = f"Retrying in {delay}s ({attempt + 1}/{MAX_TASK_RETRIES})…"
                logger.info(
                    f"Download task {task.id} ('{task.title}') transient failure ({task.error}). "
                    f"Auto-retry attempt {attempt + 1}/{MAX_TASK_RETRIES} in {delay}s..."
                )
                task.speed_bps = 0.0
                task.eta_seconds = 0

                # Hand the slot back for the duration of the wait, otherwise a
                # backing-off task keeps queued downloads stuck at "Pending".
                self._running_task_ids.discard(task.id)
                holds_slot = False
                asyncio.create_task(self._pump_queue())

                await asyncio.sleep(delay)
                if task.status != "retrying":
                    break                          # cancelled/paused/removed mid-wait
        finally:
            self._running_task_ids.discard(task.id)
            if task.status == "retrying":           # never leave this on screen
                task.status = "failed"
            self._save_history()
            if task.status == "completed" and self.on_task_completed:
                try:
                    self.on_task_completed(task)
                except Exception as cb_err:
                    logger.debug(f"on_task_completed callback error: {cb_err}")
            asyncio.create_task(self._pump_queue())

    # Failures that will never resolve on their own. Retrying them just replays
    # the whole fallback cascade and delays the message the user needs to see.
    _FATAL_PHRASES = (
        "404", "not found", "http error 404", "status 404",
        "private or age-restricted", "login required", "age-restricted",
        "video not found", "has been deleted", "no longer available",
        "not supported", "unsupported url", "no media was found",
        "requested format is not available", "quality is no longer available",
        "drm", "protected", "copyright",
        "ffmpeg is needed", "ffprobe",
        "cannot write to the download folder", "permission denied",
        "no space left", "disk full", "not enough space",
    )

    @classmethod
    def _is_fatal(cls, error_msg: str) -> bool:
        """True when retrying cannot possibly help."""
        low = (error_msg or "").lower()
        return any(phrase in low for phrase in cls._FATAL_PHRASES)

    @staticmethod
    def _is_retryable(error_msg: str) -> bool:
        """Return True if the error is a transient network issue worth retrying."""
        low = (error_msg or "").lower()
        retryable_phrases = (
            "network error", "timed out", "timeout", "connection",
            "unable to download", "rate limited", "http error 429",
            "http error 503", "http error 502", "http error 504", "http error 500",
            "503", "502", "504",
            "too many requests", "retrying", "socket", "errno 11",
            "urlopen error", "incomplete read", "reset by peer",
            "broken pipe", "ssl", "certificate", "stalled", "no response",
            "temporary failure", "network is unreachable", "connection refused",
            "service temporarily unavailable", "gateway timeout",
        )
        return any(phrase in low for phrase in retryable_phrases)

    @staticmethod
    def _cleanup_partial(file_path: str) -> None:
        """Remove incomplete temporary download files (.part, .ytdl, etc.) so retries start fresh."""
        if not file_path:
            return
        dirs_to_check = [os.path.dirname(file_path)]
        try:
            from .binaries import data_dir
            t_dir = os.path.join(data_dir(), "temp_downloads")
            if os.path.isdir(t_dir) and t_dir not in dirs_to_check:
                dirs_to_check.append(t_dir)
        except Exception:
            pass

        base_name = os.path.basename(file_path)
        for d in dirs_to_check:
            if not d or not os.path.isdir(d):
                continue
            for ext in ("", ".part", ".ytdl", ".temp", ".aria2", ".part.meta.json"):
                if ext == "":
                    # Only remove naked filename if it's inside the temporary downloads directory
                    if d != dirs_to_check[0]:
                        p = os.path.join(d, base_name)
                    else:
                        continue
                else:
                    p = os.path.join(d, base_name + ext)
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass

    def get_task(self, download_id: str) -> Optional[DownloadTask]:
        return self._tasks.get(download_id)

    def list_tasks(self) -> list[DownloadTask]:
        return list(self._tasks.values())

    def get_queue(self) -> list[DownloadTask]:
        """Return list of tasks currently waiting in the dispatch queue in order."""
        return [entry[0] for entry in self._queue]

    def get_queue_position(self, download_id: str) -> int:
        """Return 1-based index of task in the pending queue, or 0 if not queued."""
        for idx, entry in enumerate(self._queue, 1):
            if entry[0].id == download_id:
                return idx
        return 0

    def move_queued_task(self, download_id: str, direction: str) -> bool:
        """Move a pending task up, down, to top, or to bottom in the queue.

        direction: 'up' | 'down' | 'top' | 'bottom'
        """
        queue_list = list(self._queue)
        target_idx = None
        for i, entry in enumerate(queue_list):
            if entry[0].id == download_id:
                target_idx = i
                break
        if target_idx is None:
            return False

        entry = queue_list.pop(target_idx)
        if direction == "up":
            new_idx = max(0, target_idx - 1)
            queue_list.insert(new_idx, entry)
        elif direction == "down":
            new_idx = min(len(queue_list), target_idx + 1)
            queue_list.insert(new_idx, entry)
        elif direction == "top":
            queue_list.insert(0, entry)
        elif direction == "bottom":
            queue_list.append(entry)
        else:
            queue_list.insert(target_idx, entry)
            return False

        self._queue = deque(queue_list)
        self._update_queue_positions()
        return True

    def reorder_queue(self, task_ids: list[str]) -> bool:
        """Reorder pending tasks in the queue to match the specified task_ids order."""
        queue_dict = {entry[0].id: entry for entry in self._queue}
        new_queue = []
        for tid in task_ids:
            if tid in queue_dict:
                new_queue.append(queue_dict.pop(tid))
        for entry in self._queue:
            if entry[0].id in queue_dict:
                new_queue.append(entry)
                queue_dict.pop(entry[0].id, None)
        self._queue = deque(new_queue)
        self._update_queue_positions()
        return True

    async def cancel_download(self, download_id: str) -> bool:
        task = self._tasks.get(download_id)
        if not task:
            return False
        if task.status in ("completed", "failed"):
            return False
        task.status = "cancelled"
        task.speed_bps = 0.0
        task.eta_seconds = 0
        self._running_task_ids.discard(download_id)
        # Purge from waiting queue immediately
        self._queue = deque([entry for entry in self._queue if entry[0].id != download_id])
        self._update_queue_positions()
        for proc in self._processes.get(download_id, []):
            _safe_kill_process(proc)
        self._processes.pop(download_id, None)
        # Partial files (.part, .aria2, .part.meta.json) are kept on disk
        # so clicking "Retry" resumes without losing progress.
        self._release_copy_index(task)
        self._save_history()
        asyncio.create_task(self._pump_queue())
        return True

    async def pause_download(self, download_id: str) -> bool:
        """Pause an active download, keeping partial fragments intact."""
        task = self._tasks.get(download_id)
        if not task or task.status not in ("downloading", "pending", "preparing", "retrying"):
            return False
        task.status = "paused"
        task.speed_bps = 0.0
        task.eta_seconds = 0
        self._running_task_ids.discard(download_id)
        # Purge from waiting queue if it was pending
        self._queue = deque([entry for entry in self._queue if entry[0].id != download_id])
        self._update_queue_positions()
        for proc in self._processes.get(download_id, []):
            _safe_kill_process(proc)
        self._processes.pop(download_id, None)
        self._save_history()
        asyncio.create_task(self._pump_queue())
        return True

    async def resume_download(self, download_id: str) -> bool:
        """Resume a paused download."""
        task = self._tasks.get(download_id)
        if not task or task.status != "paused":
            return False
        task.status = "pending"
        task.error = ""
        params = getattr(task, "_run_params", None)
        if params:
            self._queue.append((task, *params))
        else:
            # Restored from history after a restart, so the original run
            # parameters are gone. task.output_dir is persisted, so use it
            # rather than dropping the file into a different folder.
            output_dir = task.output_dir or os.path.join(os.path.expanduser("~"), "Downloads")
            cdn_referer = cdn_referer_for_host(task.video_url)
            self._queue.append((task, output_dir, "", "", cdn_referer))
        self._update_queue_positions()
        self._save_history()
        asyncio.create_task(self._pump_queue())
        return True

    async def retry_download(self, download_id: str) -> bool:
        """Retry a failed or cancelled download, resuming from existing partial bytes."""
        task = self._tasks.get(download_id)
        if not task or task.status not in ("failed", "cancelled"):
            return False
        task.status = "pending"
        task.error = ""
        task.speed_bps = 0.0
        task.eta_seconds = 0
        params = getattr(task, "_run_params", None)
        if params:
            self._queue.append((task, *params))
        else:
            output_dir = task.output_dir or os.path.join(os.path.expanduser("~"), "Downloads")
            cdn_referer = cdn_referer_for_host(task.video_url)
            self._queue.append((task, output_dir, "", "", cdn_referer))
        self._update_queue_positions()
        self._save_history()
        asyncio.create_task(self._pump_queue())
        return True

    async def pause_all(self) -> int:
        """Pause all active and pending downloads, saving partial progress."""
        paused_count = 0
        to_pause = [
            t for t in self._tasks.values()
            if t.status in ("downloading", "pending", "preparing", "retrying")
        ]
        self._queue.clear()
        self._update_queue_positions()

        for task in to_pause:
            task.status = "paused"
            task.speed_bps = 0.0
            task.eta_seconds = 0
            self._running_task_ids.discard(task.id)
            for proc in self._processes.get(task.id, []):
                _safe_kill_process(proc)
            self._processes.pop(task.id, None)
            paused_count += 1

        self._save_history()
        return paused_count

    async def resume_all(self) -> int:
        """Resume all paused downloads, re-queuing them respecting concurrency/sequential limits."""
        resumed_count = 0
        paused_tasks = [
            t for t in self._tasks.values()
            if t.status == "paused"
        ]
        for task in paused_tasks:
            task.status = "pending"
            task.error = ""
            task.speed_bps = 0.0
            task.eta_seconds = 0
            params = getattr(task, "_run_params", None)
            if params:
                self._queue.append((task, *params))
            else:
                output_dir = task.output_dir or os.path.join(os.path.expanduser("~"), "Downloads")
                cdn_referer = cdn_referer_for_host(task.video_url)
                self._queue.append((task, output_dir, "", "", cdn_referer))
            resumed_count += 1

        self._update_queue_positions()
        self._save_history()
        asyncio.create_task(self._pump_queue())
        return resumed_count

    def remove_task(self, download_id: str) -> bool:
        """Remove an individual download task from history and queue without deleting the completed file."""
        task = self._tasks.get(download_id)
        if not task:
            return False
        is_completed = task.status == "completed"
        task.status = "cancelled"
        task.speed_bps = 0.0
        task.eta_seconds = 0
        self._running_task_ids.discard(download_id)
        # Purge from waiting queue
        self._queue = deque([entry for entry in self._queue if entry[0].id != download_id])
        self._update_queue_positions()
        for proc in self._processes.get(download_id, []):
            _safe_kill_process(proc)
        self._processes.pop(download_id, None)
        if not is_completed:
            self._cleanup_partial(task.file_path)
        self._release_copy_index(task)
        self._tasks.pop(download_id, None)
        self._save_history()
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._pump_queue())
        except RuntimeError:
            pass
        return True

    def clear_completed(self, only_completed: bool = False) -> int:
        targets = ("completed",) if only_completed else ("completed", "failed", "cancelled")
        done = [
            tid for tid, t in self._tasks.items()
            if t.status in targets
        ]
        for tid in done:
            del self._tasks[tid]
        self._save_history()
        return len(done)

    def clear_incomplete(self, include_pending: bool = False) -> int:
        """Clear all incomplete (failed, cancelled, paused) download tasks and purge their temporary files.
        If include_pending is True, also clears waiting queued tasks."""
        targets = ("failed", "cancelled", "paused")
        if include_pending:
            targets = ("failed", "cancelled", "paused", "pending")

        incomplete_ids = [
            tid for tid, t in self._tasks.items()
            if t.status in targets
        ]
        if not incomplete_ids:
            return 0

        for tid in incomplete_ids:
            task = self._tasks.get(tid)
            if not task:
                continue
            task.status = "cancelled"
            task.speed_bps = 0.0
            task.eta_seconds = 0
            self._running_task_ids.discard(tid)
            for proc in self._processes.get(tid, []):
                _safe_kill_process(proc)
            self._processes.pop(tid, None)
            self._cleanup_partial(task.file_path)
            self._release_copy_index(task)
            self._tasks.pop(tid, None)

        self._queue = deque([entry for entry in self._queue if entry[0].id not in incomplete_ids])
        self._update_queue_positions()
        self._save_history()
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._pump_queue())
        except RuntimeError:
            pass
        return len(incomplete_ids)

    # ── Command construction ──────────────────────────────────────

    def _build_argv(
        self,
        task: DownloadTask,
        output_dir: str,
        cookies_browser: str,
        cookies_file: str,
        aria2c: Optional[str],
        ffmpeg: Optional[str],
        cdn_referer: str = "",
    ) -> list[str]:
        selector = task.format_id or "bestvideo+bestaudio/best"
        audio_mp3 = False
        if selector.startswith("mp3:"):
            audio_mp3 = True
            selector = selector[4:] or "bestaudio/best"
        elif selector in ("best", "max", ""):
            selector = "bestvideo+bestaudio/best"
        elif task.needs_merge and "+" not in selector and not selector.startswith("bestaudio"):
            selector = f"{selector}+bestaudio[ext=m4a]/{selector}+bestaudio/{selector}/best"

        argv = [*ytdlp_argv(), *js_runtime_argv(), "-f", selector]
        argv += ["--format-sort", "res,vcodec:h264,acodec:m4a"]

        host = urlparse(task.video_url).hostname if task.video_url else ""
        platform = find_platform(host)
        if platform and platform.extractor_args:
            argv += ["--extractor-args", platform.extractor_args]

        # Standard Browser User-Agent to prevent Facebook / Instagram anti-bot & "Cannot parse data" blocks.
        # Do not override User-Agent for YouTube because YouTube's Innertube API validates client-specific UAs (e.g. Android/VisionOS).
        if not (platform and platform.id == "youtube"):
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            argv += ["--user-agent", ua]

        # Repeat downloads of the same video get " (1)", " (2)", … appended
        # after the name, so every save lands in its own file instead of
        # yt-dlp skipping with "has already been downloaded". copy_index is
        # fixed once per task, so retries keep the same target filename.
        quality_badge = _clean_quality_tag(task)
        quality_suffix = f" [{quality_badge}]" if quality_badge else ""
        copy_suffix = f" ({task.copy_index})" if task.copy_index > 0 else ""
        if task.index > 0:
            # Dynamic padding: 2 digits for ≤99, 3 for ≤999, 4 for ≤9999
            batch_total = sum(1 for t in self._tasks.values() if t.batch_id and t.batch_id == task.batch_id)
            pad = max(2, len(str(max(task.index, batch_total))))
            filename_tpl = f"{task.index:0{pad}d} - %(title).180B{quality_suffix}{copy_suffix}.%(ext)s"
        else:
            filename_tpl = f"%(title).180B{quality_suffix}{copy_suffix}.%(ext)s"

        temp_dir = os.path.join(data_dir(), "temp_downloads")
        os.makedirs(temp_dir, exist_ok=True)

        argv += [
            "--paths", f"home:{output_dir}",
            "--paths", f"temp:{temp_dir}",
            "--newline",
            "--progress",
            "--windows-filenames",
            "--no-playlist",
            "--no-warnings",
            "--no-check-formats",      # Skip redundant format checking — starts download instantly!
            "--no-write-subs",
            "--no-write-comments",
            "--no-write-thumbnail",
            "--compat-options", "no-youtube-unavailable-videos",
            "--part",                  # write intermediate parts in temp_dir
            "--no-mtime",
            "--continue",              # resume partial downloads seamlessly
            "--socket-timeout", "15",
            # Kept modest on purpose: yt-dlp's internal retries are multiplied by
            # the fallback cascade in _run() and by the task-level retry, so a
            # large budget here is what turned a dead link into a 20-minute wait.
            "--retries", "3",
            "--fragment-retries", "5",
            "--retry-sleep", "linear=1::2",    # 1s, 3s, 5s… between retries
            "--file-access-retries", "3",
            "--concurrent-fragments", str(CONCURRENT_FRAGMENTS),
            "--progress-delta", "0.2", # Emit smooth progress updates every 200ms (5x/sec)
            # Rate below which yt-dlp assumes throttling and re-extracts. 100K
            # is above what a genuinely slow link can sustain, which made it
            # re-extract in a loop and never finish; 32K only trips on the real
            # YouTube throttle.
            "--throttled-rate", "32K",
            "--merge-output-format", "mp4",
            "-o", filename_tpl,
        ]

        if audio_mp3:
            argv += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]

        if self.rate_limiter.speed_limit_kbps > 0:
            argv += ["--limit-rate", f"{self.rate_limiter.speed_limit_kbps}K"]

        if aria2c:
            # aria2c only gets plain progressive HTTP(S) streams. Fragmented
            # HLS/DASH stays on yt-dlp's native downloader: aria2c would fetch
            # each fragment as a separate job and report no useful progress,
            # while native + --concurrent-fragments is both faster and visible.
            #
            # Only these protocol keys exist: http, ftp, m3u8, dash, rtsp, rtmp,
            # mms. yt-dlp normalises the real protocol before the lookup, so
            # "http" also covers https and "m3u8" covers m3u8_native. Naming a
            # key it does not know (e.g. "https" or "m3u8_native") makes yt-dlp
            # read the *entire* argument as a downloader name and abort with
            # `No such external downloader "dash,m3u8,m3u8_native:native"`.
            aria2_limit = f" --max-download-limit={self.rate_limiter.speed_limit_kbps}K" if self.rate_limiter.speed_limit_kbps > 0 else ""
            argv += [
                "--downloader", f"http,ftp:{aria2c}",
                "--downloader", "dash,m3u8:native",
                "--downloader-args", (
                    f"aria2c:--max-connection-per-server={ARIA2_CONNECTIONS} "
                    f"--split={ARIA2_SPLIT} --min-split-size={ARIA2_MIN_SPLIT} "
                    "--summary-interval=1 --console-log-level=warn "
                    "--download-result=hide --show-console-readout=true "
                    "--file-allocation=none --retry-wait=2 --max-tries=5 "
                    "--connect-timeout=15 --timeout=30 "
                    f"--auto-file-renaming=false --allow-overwrite=true{aria2_limit}"
                ),
            ]

        if ffmpeg:
            argv += ["--ffmpeg-location", ffmpeg]

        if cookies_browser:
            argv += ["--cookies-from-browser", cookies_browser]
        if cookies_file and os.path.isfile(cookies_file):
            argv += ["--cookies", cookies_file]
        
        referer = cdn_referer or cdn_referer_for_host(task.video_url)
        if referer:
            argv += ["--referer", referer]

        argv += ["--", task.video_url]
        return argv

    # ── Core run ──────────────────────────────────────────────────

    async def _run(
        self,
        task: DownloadTask,
        output_dir: str,
        cookies_browser: str,
        cookies_file: str,
        cdn_referer: str = "",
    ):
        output_dir = output_dir or os.path.join(os.path.expanduser("~"), "Downloads")
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as exc:
            task.status = "failed"
            task.error = f"Cannot write to the download folder: {exc}"
            return

        try:
            task.error = ""

            # ffmpeg is only genuinely required when two streams have to be
            # merged or audio has to be transcoded. Everything else must not sit
            # behind a possible ~40 MB ffmpeg download, so ask for it without
            # waiting (timeout=0 = "use it if it is already installed").
            selector = task.format_id or ""
            # Direct streams are saved byte-for-byte by _download_direct below,
            # which never shells out to ffmpeg — including the TikTok "mp3:"
            # case, where the CDN URL already points at an mp3.
            is_direct_only = selector in ("direct_stream", "mp3:direct_stream")
            needs_ffmpeg = not is_direct_only and (
                task.needs_merge
                or "+" in selector
                or selector.startswith("mp3:")
            )
            if needs_ffmpeg:
                # If ffmpeg is already installed, proceed straight to downloading without delay
                ffmpeg = await ensure_ffmpeg(timeout=0)
                if not ffmpeg:
                    task.status = "preparing"
                    ffmpeg = await ensure_ffmpeg(timeout=FFMPEG_WAIT_S)
                if not ffmpeg:
                    task.status = "failed"
                    task.error = ("FFmpeg is needed to finish this download. "
                                  "Install it from Settings → Engine, or pick a "
                                  "single-file quality such as 720p.")
                    return
                task.status = "downloading"
            else:
                task.status = "downloading"
                ffmpeg = await ensure_ffmpeg(timeout=0)
            f_dir = ffmpeg_dir() if ffmpeg else None

            # Immediate Segmented Download:
            # If a direct media URL is already available for self-contained media (no separate merge needed),
            if (
                task.direct_url
                and not _is_manifest_url(task.direct_url)
                and (is_direct_only or (task.has_audio and not task.needs_merge))
            ):
                logger.info(f"Triggering instant segmented download: {task.direct_url}")
                await self._download_direct(task, output_dir, cdn_referer=cdn_referer)

                if task.status == "completed":
                    return
                if task.status in ("cancelled", "paused"):
                    return

            plat = (task.platform or "").lower()
            url = (task.video_url or "").lower()
            is_strict_cdn = any(
                p in plat or p in url
                for p in (
                    "facebook", "fb.watch", "fb.com",
                    "instagram",
                    "tiktok",
                    "twitter", "x.com",
                    "reddit",
                    "youtube", "youtu.be", "googlevideo",
                )
            )

            aria2c = None if is_strict_cdn else aria2c_path()

            await self._download(
                task, output_dir, cookies_browser, cookies_file,
                aria2c=aria2c,
                ffmpeg=f_dir,
                cdn_referer=cdn_referer,
            )
            # If external downloader was attempted and failed, retry with native yt-dlp
            if task.status == "failed" and aria2c:
                logger.info(
                    f"Download failed via external downloader ({task.error}). "
                    "Retrying with yt-dlp native downloader."
                )
                task.error = ""
                await self._download(
                    task, output_dir, cookies_browser, cookies_file,
                    aria2c=None,
                    ffmpeg=f_dir,
                    cdn_referer=cdn_referer,
                )

            # Direct-stream fallback only for self-contained media. A merged
            # (video+audio) selection has no single URL with both tracks, so
            # falling back there would save a muted video-only stream.
            if (
                task.status == "failed"
                and task.direct_url
                and not _is_manifest_url(task.direct_url)
                and task.has_audio
                and not task.needs_merge
            ):
                logger.info(f"Primary download failed ({task.error}). Attempting direct media stream fallback: {task.direct_url}")
                await self._download_direct(task, output_dir, cdn_referer=cdn_referer)


            # A fatal error (private video, dead link, unsupported URL, no disk
            # space…) will fail identically on every remaining path, so report
            # it now instead of spending minutes proving it three more times.
            if task.status == "failed" and self._is_fatal(task.error):
                return

            # ── Final yt-dlp re-extract retry ─────────────────────────────────
            # Only for non-merge (self-contained) formats — merged formats like
            # YouTube 1440p/1080p are handled correctly by yt-dlp with its own
            # format selector and do NOT need re-extraction.
            if (
                task.status == "failed"
                and task.video_url
                and not task.needs_merge
                and not self._is_fatal(task.error)
            ):
                logger.info(f"All attempts failed. Attempting full re-extract retry for: {task.video_url}")
                try:
                    from .extractor import extract as _re_extract_main
                    fresh_result = await _re_extract_main(task.video_url)
                    if fresh_result and fresh_result.formats:
                        # Update direct_url with freshly extracted URL
                        for fmt in fresh_result.formats:
                            if getattr(fmt, "format_id", "") == task.format_id and getattr(fmt, "url", ""):
                                task.direct_url = fmt.url
                                break
                        # Reset task state and run yt-dlp one more time
                        task.error = ""
                        task.progress = 0.0
                        task.downloaded_bytes = 0
                        task.total_bytes = 0
                        await self._download(
                            task, output_dir, cookies_browser, cookies_file,
                            aria2c=None,
                            ffmpeg=f_dir,
                            cdn_referer=cdn_referer,
                        )
                except Exception as re_err:
                    logger.debug(f"Final re-extract retry failed: {re_err}")
        except Exception as exc:
            if task.status not in ("cancelled", "completed", "paused"):
                if task.direct_url and task.has_audio and not task.needs_merge:
                    try:
                        logger.info(f"Primary download raised ({exc}). Attempting direct media stream fallback: {task.direct_url}")
                        await self._download_direct(task, output_dir, cdn_referer=cdn_referer)
                        return
                    except Exception as direct_exc:
                        logger.warning(f"Direct stream download also failed: {direct_exc}")
                task.status = "failed"
                task.error = str(exc)
            # Partial files (.part, .aria2, .part.meta.json) are kept on disk
            # so automatic and manual retries resume without wasting bandwidth.
        finally:
            self._processes.pop(task.id, None)

    async def _download_direct(
        self,
        task: DownloadTask,
        output_dir: str,
        cdn_referer: str = "",
    ):
        """Accelerated direct media stream download using SegmentedDownloader with multi-strategy header fallback."""
        task.status = "downloading"
        task.engine = "direct"
        task.error = ""
        ext = "mp4"
        if task.format_label and "mp3" in task.format_label.lower():
            ext = "mp3"
        elif task.format_id and "mp3" in task.format_id.lower():
            ext = "mp3"

        safe_title = re.sub(r'[\\/*?:"<>|]', "", task.title or "media").strip()[:120]
        quality_badge = _clean_quality_tag(task)
        quality_suffix = f" [{quality_badge}]" if quality_badge else ""
        # Same naming rule as the yt-dlp path: repeat saves get " (1)", " (2)"…
        copy_suffix = f" ({task.copy_index})" if task.copy_index > 0 else ""
        if task.index > 0:
            file_name = f"{task.index:02d} - {safe_title}{quality_suffix}{copy_suffix}.{ext}"
        elif copy_suffix:
            file_name = f"{safe_title}{quality_suffix}{copy_suffix}.{ext}"
        else:
            file_name = f"{safe_title}{quality_suffix}.{ext}"
            dest_path = os.path.join(output_dir, file_name)
            if os.path.exists(dest_path):
                count = 1
                while os.path.exists(os.path.join(output_dir, f"{safe_title}{quality_suffix} ({count}).{ext}")):
                    count += 1
                file_name = f"{safe_title}{quality_suffix} ({count}).{ext}"

        dest_path = os.path.join(output_dir, file_name)
        task.file_path = dest_path

        # Multi-strategy headers to bypass CDN hotlinking / 403 blocks across different CDNs
        stream_is_tikwm = "tikwm.com" in (task.direct_url or "").lower()
        if stream_is_tikwm:
            ref = "https://www.tikwm.com/"
        else:
            ref = cdn_referer or ("https://www.tiktok.com/" if "tiktok" in (task.video_url or "").lower() else "")

        header_strategies = [
            # Strategy 1: Desktop Browser with Referer (most reliable across TikTok, TikWM, Instagram, Twitter CDN)
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Referer": ref,
                "Accept": "*/*",
            },
            # Strategy 2: Clean Desktop Browser without Referer
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "*/*",
            },
            # Strategy 3: Mobile App UA
            {
                "User-Agent": "okhttp/3.14.9",
                "Accept": "*/*",
            },
        ]

        async def _try_download(url: str) -> bool:
            for headers in header_strategies:
                if task.status in ("cancelled", "paused"):
                    return False

                def _progress_cb(downloaded: int, total: int, speed: float, eta: int):
                    task.downloaded_bytes = downloaded
                    if total > 0:
                        task.total_bytes = total
                        task.progress = min(99.0, (downloaded / total) * 100.0)
                    task.speed_bps = speed
                    task.eta_seconds = eta

                downloader = SegmentedDownloader(
                    url=url,
                    dest_path=dest_path,
                    headers=headers,
                    max_connections=16,
                    rate_limiter=self.rate_limiter,
                    on_progress=_progress_cb,
                    is_cancelled=lambda: task.status == "cancelled",
                    is_paused=lambda: task.status == "paused",
                )

                try:
                    success = await downloader.download()
                    if success and os.path.isfile(dest_path):
                        task.status = "completed"
                        task.progress = 100.0
                        task.speed_bps = 0.0
                        task.eta_seconds = 0
                        task.total_bytes = os.path.getsize(dest_path)
                        task.downloaded_bytes = task.total_bytes
                        return True
                except Exception as e:
                    logger.debug(f"Direct stream download strategy error: {e}")

                if task.status in ("cancelled", "paused"):
                    return False

            return False

        # Attempt 1: primary direct URL with header strategies
        if task.direct_url and await _try_download(task.direct_url):
            return

        if task.status in ("cancelled", "paused"):
            return

        # Attempt 2: Fresh TikTok stream fetch if applicable
        if "tiktok" in (task.platform or "").lower() or "tiktok" in (task.video_url or "").lower():
            try:
                from .gateways.tiktok_adapter import tiktok_adapter
                fresh = await tiktok_adapter.extract(task.video_url)
                if fresh and fresh.formats and fresh.formats[0].url:
                    fresh_url = fresh.formats[0].url
                    task.direct_url = fresh_url
                    if await _try_download(fresh_url):
                        return
            except Exception as re_exc:
                logger.debug(f"Fresh TikTok stream fetch failed: {re_exc}")

        if task.status in ("cancelled", "paused"):
            return

        # Attempt 3: Universal re-extract fallback
        try:
            logger.info(f"Direct URL exhausted. Re-extracting fresh URL for: {task.video_url}")
            from .extractor import extract as _re_extract
            fresh_result = await _re_extract(task.video_url)
            fresh_url = ""
            if fresh_result and fresh_result.formats:
                for fmt in fresh_result.formats:
                    if getattr(fmt, "format_id", "") == task.format_id and getattr(fmt, "url", ""):
                        fresh_url = fmt.url
                        break
                if not fresh_url:
                    for fmt in fresh_result.formats:
                        if getattr(fmt, "has_audio", True) and not getattr(fmt, "needs_merge", False) and getattr(fmt, "url", ""):
                            fresh_url = fmt.url
                            break
                if not fresh_url and fresh_result.formats[0].url:
                    fresh_url = fresh_result.formats[0].url

            if fresh_url:
                task.direct_url = fresh_url
                if await _try_download(fresh_url):
                    return
        except Exception as reex_err:
            logger.debug(f"Re-extract fallback failed: {reex_err}")

        if task.status in ("cancelled", "paused", "completed"):
            return
        task.status = "failed"
        task.speed_bps = 0.0
        task.eta_seconds = 0
        task.error = "Could not download direct media stream. The media link may have expired."

    # yt-dlp: "[download]  42.1% of  12.34MiB at  3.21MiB/s ETA 00:03" or "100% of 15.20MiB in 00:04 at 3.50MiB/s"
    _YT_PROG = re.compile(
        r"\[download\]\s+(?P<pct>[0-9.]+)%\s+of\s+~?\s*(?P<size>[0-9.]+)(?P<sz_u>[KMGT]iB|[KMGT]B|B)"
        r"(?:\s+(?:at|in)\s+(?:[0-9:]+\s+at\s+)?(?P<spd>[0-9.]+)(?P<spd_u>[KMGT]iB|[KMGT]B|B)/s)?"
        r"(?:\s+ETA\s+(?P<eta>[0-9:]+))?"
    )
    # aria2c readout: "[#1a2b3c 3.5MiB/45MiB(7%) CN:16 DL:4.2MiB ETA:9s]"
    _ARIA_PROG = re.compile(
        r"\[#\w+\s+(?P<dl>[0-9.]+)(?P<dl_u>[KMGT]iB|[KMGT]B|B)/(?P<tot>[0-9.]+)(?P<tot_u>[KMGT]iB|[KMGT]B|B)"
        r"\((?P<pct>[0-9]+)%\).*?DL:(?P<spd>[0-9.]+)(?P<spd_u>[KMGT]iB|[KMGT]B|B)(?:/s)?"
        r"(?:.*?ETA:(?P<eta>[0-9dhms]+))?"
    )
    _DEST = re.compile(r"\[download\] Destination: (?P<path>.+)")
    _ALREADY = re.compile(r"\[download\] (?P<path>.+) has already been downloaded")
    _MERGE = re.compile(r'\[Merger\] Merging formats into "(?P<path>.+)"')
    _MOVE = re.compile(r'\[MoveFiles\] Moving file ".*?" to "(?P<path>.+)"')
    _EXTRACT = re.compile(r"\[ExtractAudio\] Destination: (?P<path>.+)")
    _SELECTED = re.compile(r"Downloading \d+ format\(s\): (?P<ids>\S+)")

    async def _download(
        self,
        task: DownloadTask,
        output_dir: str,
        cookies_browser: str,
        cookies_file: str,
        aria2c: Optional[str],
        ffmpeg: Optional[str],
        cdn_referer: str = "",
    ):
        task.status = "downloading"
        task.engine = "aria2c" if aria2c else "ytdlp"
        argv = self._build_argv(
            task, output_dir, cookies_browser, cookies_file, aria2c, ffmpeg,
            cdn_referer=cdn_referer,
        )

        temp_dir = os.path.join(str(data_dir()), "temp_downloads")
        os.makedirs(temp_dir, exist_ok=True)
        env = dict(os.environ)
        app_bin = str((app_dir() / "bin").resolve())
        bin_paths = [p for p in [app_bin, ffmpeg] if p and os.path.isdir(p)]
        if bin_paths:
            env["PATH"] = os.pathsep.join(bin_paths) + os.pathsep + env.get("PATH", "")

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=temp_dir,
            env=env,
            **popen_kwargs(),
        )
        self._processes.setdefault(task.id, []).append(proc)

        # Shared with the watchdog: "last" is bumped on every byte of output.
        io_state: dict = {"last": asyncio.get_running_loop().time(), "stalled": 0}
        watchdog = asyncio.create_task(self._stall_watchdog(proc, task, io_state))

        tail: deque[str] = deque(maxlen=60)
        streams = 2 if ("+" in (task.format_id or "")) else 1  # how many files this format needs
        stream_index = -1  # index of the file currently downloading
        # Per-stream (downloaded, total).
        sizes: dict[int, tuple[int, int]] = {}

        def record(dl: int, total: int, pct_override: Optional[float] = None) -> None:
            idx = max(stream_index, 0)
            sizes[idx] = (dl, total)
            task.downloaded_bytes = sum(d for d, _ in sizes.values())

            known_total = sum(t for _, t in sizes.values())
            v_tot = sizes.get(0, (0, 0))[1]
            a_tot = sizes.get(1, (0, 0))[1]

            if streams == 2:
                if v_tot > 0 and a_tot > 0:
                    task.total_bytes = v_tot + a_tot
                elif v_tot > 0:
                    dur = getattr(task, "duration", 0) or 0
                    est_audio = int(dur * 16000) if dur > 0 else int(v_tot * 0.08)
                    task.total_bytes = v_tot + max(est_audio, int(v_tot * 0.08))
                elif known_total > 0:
                    task.total_bytes = known_total
                else:
                    exp = getattr(task, "expected_bytes", 0)
                    if exp > 0:
                        task.total_bytes = exp
            elif known_total > 0:
                task.total_bytes = known_total
            else:
                exp = getattr(task, "expected_bytes", 0)
                if exp > 0:
                    task.total_bytes = exp

            if task.total_bytes > 0:
                calc_pct = (task.downloaded_bytes / task.total_bytes) * 99.0
                task.progress = max(task.progress, min(99.0, max(0.0, calc_pct)))
            elif pct_override is not None:
                task.progress = max(task.progress, min(99.0, max(0.0, pct_override)))

        async for line in self._lines(proc, task, io_state):
            if not line:
                continue
            tail.append(line)

            m = self._SELECTED.search(line)
            if m:
                streams = max(1, len(m.group("ids").split("+")))
                continue

            m = self._DEST.search(line) or self._ALREADY.search(line)
            if m:
                # If moving to the next stream, ensure previous stream was recorded at full
                if stream_index >= 0 and stream_index in sizes:
                    prev_dl, prev_tot = sizes[stream_index]
                    if prev_tot > 0:
                        sizes[stream_index] = (prev_tot, prev_tot)
                stream_index += 1
                path = m.group("path").strip()
                if streams == 1 or stream_index == 0:
                    task.file_path = path
                continue

            m = self._MOVE.search(line) or self._MERGE.search(line) or self._EXTRACT.search(line)
            if m:
                task.file_path = m.group("path").strip().strip('"')
                task.status = "merging"
                task.progress = 99.0
                task.speed_bps = 0.0
                task.eta_seconds = 0
                if task.total_bytes > 0:
                    task.downloaded_bytes = task.total_bytes
                continue

            if "[Merger]" in line or "[ExtractAudio]" in line or "[FixupM3u8]" in line:
                task.status = "merging"
                task.progress = 99.0
                task.speed_bps = 0.0
                task.eta_seconds = 0
                if task.total_bytes > 0:
                    task.downloaded_bytes = task.total_bytes
                continue

            m = self._YT_PROG.search(line)
            if m:
                pct = float(m.group("pct"))
                total = _bytes_from_value(m.group("size"), m.group("sz_u"))
                dl_bytes = int(total * pct / 100)
                record(dl_bytes, total, pct_override=pct)
                if m.group("spd"):
                    new_speed = float(_bytes_from_value(m.group("spd"), m.group("spd_u")))
                    if new_speed > 0:
                        if task.speed_bps <= 0:
                            task.speed_bps = new_speed
                        else:
                            task.speed_bps = (0.65 * task.speed_bps) + (0.35 * new_speed)
                if m.group("eta"):
                    task.eta_seconds = _seconds_from_eta(m.group("eta"))
                if task.status == "merging":
                    task.status = "downloading"
                continue

            m = self._ARIA_PROG.search(line)
            if m:
                dl = _bytes_from_value(m.group("dl"), m.group("dl_u"))
                total = _bytes_from_value(m.group("tot"), m.group("tot_u"))
                pct = float(m.group("pct"))
                record(dl, total, pct_override=pct)
                if m.group("spd"):
                    new_speed = float(_bytes_from_value(m.group("spd"), m.group("spd_u")))
                    if new_speed > 0:
                        if task.speed_bps <= 0:
                            task.speed_bps = new_speed
                        else:
                            task.speed_bps = (0.65 * task.speed_bps) + (0.35 * new_speed)
                if m.group("eta"):
                    task.eta_seconds = _parse_aria2_eta(m.group("eta") or "")
                if task.status == "merging":
                    task.status = "downloading"

        await proc.wait()
        watchdog.cancel()

        # "paused" was missing here, so pausing killed the process and then the
        # non-zero exit code immediately overwrote the status with "failed".
        if task.status in ("cancelled", "paused"):
            return

        if io_state["stalled"]:
            task.status = "failed"
            secs = io_state["stalled"]
            if task.downloaded_bytes > 0:
                task.error = (f"Download stalled — no data for {secs}s. "
                              "The server stopped responding; try again.")
            else:
                task.error = (f"No response for {secs}s. The site or CDN never "
                              "started sending data; try again in a moment.")
            task.speed_bps = 0.0
            task.eta_seconds = 0
            self._cleanup_partial(task.file_path)
            return

        if proc.returncode == 0:
            task.status = "completed"
            task.progress = 100.0
            task.speed_bps = 0.0
            task.eta_seconds = 0
            if not task.file_path or not os.path.isfile(task.file_path) or "temp_downloads" in task.file_path:
                found = _guess_output(output_dir, task.title) or _guess_output(output_dir, task.file_path)
                if found:
                    task.file_path = found
                elif os.path.isdir(output_dir):
                    task.file_path = output_dir

            if task.file_path and os.path.isfile(task.file_path):
                try:
                    actual_sz = os.path.getsize(task.file_path)
                    task.total_bytes = actual_sz
                    task.downloaded_bytes = actual_sz
                except OSError:
                    pass
            elif task.total_bytes:
                task.downloaded_bytes = task.total_bytes
        else:
            task.status = "failed"
            task.speed_bps = 0.0
            task.eta_seconds = 0
            task.error = _friendly_error("\n".join(tail))

    async def _stall_watchdog(self, proc, task: DownloadTask, state: dict) -> None:
        """Kill the subprocess if it stops producing output.

        yt-dlp and aria2c both print progress continuously, so silence means the
        transfer is wedged (dead socket, CDN black hole). Killing it closes
        stdout, which ends ``_lines`` naturally and frees the concurrency slot —
        without this the reader awaits forever and every queued download stays
        on "Pending".
        """
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(STALL_CHECK_INTERVAL_S)
            if proc.returncode is not None:
                return                      # finished on its own
            if task.status in ("cancelled", "paused"):
                return                      # the caller already killed it
            # Post-processing is quiet by nature: ffmpeg can spend minutes
            # merging a large file without printing anything. So is the stretch
            # before the first byte, where yt-dlp is resolving the page and
            # negotiating with the CDN.
            if task.status == "merging":
                limit = MERGE_STALL_TIMEOUT_S
            elif task.downloaded_bytes <= 0:
                limit = STARTUP_STALL_TIMEOUT_S
            else:
                limit = STALL_TIMEOUT_S
            if loop.time() - state["last"] > limit:
                # max(1, …) so the flag stays truthy even if a test shrinks the
                # limit below a second.
                state["stalled"] = max(1, int(limit))
                logger.warning(f"Download stalled for {limit}s, killing: {task.title[:60]}")
                _safe_kill_process(proc)
                return

    async def _lines(self, proc, task: DownloadTask, state: Optional[dict] = None):
        """Yield output lines, splitting on both \n and \r.

        aria2c redraws its readout with carriage returns instead of newlines, so
        ``readline()`` alone would block until the download finished.
        """
        buf = b""
        while True:
            chunk = await proc.stdout.read(512)
            if not chunk:
                break
            if state is not None:
                state["last"] = asyncio.get_running_loop().time()
            if task.status in ("cancelled", "paused"):
                _safe_kill_process(proc)
                return
            buf += chunk
            buf = buf.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                yield raw.decode("utf-8", "replace").strip()
        # Flush whatever is left once the pipe closes. The old code yielded the
        # partial buffer on *every* chunk without clearing it, so the same
        # half-line was re-parsed over and over and the completed line arrived
        # again behind it. Both progress engines terminate their lines (yt-dlp
        # with --newline, aria2c with the carriage return normalised above), so
        # nothing is lost by waiting for EOF here.
        if buf:
            yield buf.decode("utf-8", "replace").strip()


# ── Utilities ────────────────────────────────────────────────────────────────

def _guess_output(output_dir: str, expected: str) -> str:
    """Find the real file in output_dir."""
    if not output_dir or not os.path.isdir(output_dir):
        return ""
    stem = os.path.splitext(os.path.basename(expected))[0] if expected else ""
    try:
        if stem:
            clean_stem = re.sub(r'[\\/*?:"<>|]', "", stem).strip()[:40]
            matches = [
                os.path.join(output_dir, name)
                for name in os.listdir(output_dir)
                if not name.endswith(('.part', '.ytdl', '.temp', '.aria2'))
                and os.path.isfile(os.path.join(output_dir, name))
                and (os.path.splitext(name)[0] == stem or (clean_stem and clean_stem in name))
            ]
            if matches:
                return max(matches, key=os.path.getmtime)

        # Fallback: get the newest media file created in output_dir
        matches = [
            os.path.join(output_dir, name)
            for name in os.listdir(output_dir)
            if not name.endswith(('.part', '.ytdl', '.temp', '.aria2'))
            and os.path.isfile(os.path.join(output_dir, name))
        ]
        if matches:
            return max(matches, key=os.path.getmtime)
    except OSError:
        pass
    return ""


def _parse_aria2_eta(eta_str: str) -> int:
    """Parse aria2c ETA strings like '1m30s', '45s', '2h'."""
    if not eta_str:
        return 0
    total = 0
    for num, unit in re.findall(r"(\d+)([dhms])", eta_str):
        n = int(num)
        total += n * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total


def _friendly_error(output: str) -> str:
    """Convert raw yt-dlp/aria2c output into a user-readable message."""
    low = output.lower()

    if any(p in low for p in ("private video", "login required", "age-restricted",
                              "sign in", "requires authentication", "--cookies")):
        return "This video is private or age-restricted. Go to Settings → Privacy & Cookies, import your signed-in cookies.txt file or select your browser (e.g. Firefox), and retry. For Instagram, try the SaveVid button."
    # Checked before the "not found" branch below, which would otherwise claim a
    # missing ffmpeg meant a missing video.
    if "ffmpeg" in low or "ffprobe" in low:
        return "FFmpeg is needed to finish this download. Install it from Settings → Engine."
    if any(p in low for p in ("video unavailable", "has been deleted", "video not found",
                              "no longer available", "404")):
        return "Video not found. It may have been deleted or removed."
    if any(p in low for p in ("unsupported url", "no video formats found", "unable to extract")):
        return "This URL is not supported, or no media was found at the link."
    if "requested format is not available" in low:
        return "That quality is no longer available. Fetch the link again and pick another option."
    if any(p in low for p in ("http error 429", "too many requests", "rate-limit")):
        return "Rate limited by the server. Wait a moment and try again."
    if any(p in low for p in ("timed out", "timeout", "connection", "network",
                              "unable to download", "errorcode=")):
        return "Network error. Check your internet connection and try again."
    if "permission denied" in low or "errno 13" in low:
        return "Cannot write to the download folder. Pick a different folder in Settings."

    for line in reversed(output.splitlines()):
        if "error" in line.lower() and len(line.strip()) > 8:
            return line.strip().removeprefix("ERROR:").strip()[:200]

    return "Download failed. Please check the URL and try again."

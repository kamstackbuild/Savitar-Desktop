"""High-performance segmented HTTP byte-range downloader for Savitar.

Provides multi-connection accelerated downloading (4-16 parallel connections)
with dynamic chunk allocation, range probe, sparse file pre-allocation,
pause/resume capability via .part and metadata, automatic non-range fallback,
and cooperative Token Bucket speed limiting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Buffer and segment tuning
MIN_SEGMENT_SIZE = 512 * 1024        # 512 KB minimum segment size
DEFAULT_CHUNK_SIZE = 512 * 1024      # 512 KB read buffer for maximum throughput
MAX_SEGMENTS = 16                    # Maximum parallel connections
MIN_SEGMENTS = 2                     # Minimum parallel connections when range enabled
MAX_WORKER_RETRIES = 4               # Per-segment retry count


@dataclass
class SegmentState:
    index: int
    start_byte: int
    end_byte: int                   # inclusive
    downloaded_bytes: int = 0
    done: bool = False

    @property
    def current_offset(self) -> int:
        return self.start_byte + self.downloaded_bytes

    @property
    def remaining_bytes(self) -> int:
        return max(0, (self.end_byte - self.start_byte + 1) - self.downloaded_bytes)

    @property
    def total_bytes(self) -> int:
        return self.end_byte - self.start_byte + 1


@dataclass
class DownloadMetadata:
    url: str
    total_bytes: int
    etag: str = ""
    last_modified: str = ""
    segments: List[Dict[str, Any]] = field(default_factory=list)

    def save(self, meta_path: str) -> None:
        try:
            tmp_path = meta_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, indent=2)
            if os.path.exists(meta_path):
                try:
                    os.replace(tmp_path, meta_path)
                except OSError:
                    os.remove(meta_path)
                    os.replace(tmp_path, meta_path)
            else:
                os.rename(tmp_path, meta_path)
        except Exception as e:
            logger.debug(f"Failed to save metadata to {meta_path}: {e}")

    @classmethod
    def load(cls, meta_path: str) -> Optional["DownloadMetadata"]:
        if not os.path.isfile(meta_path):
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls(**data)
        except Exception as e:
            logger.debug(f"Failed to load metadata from {meta_path}: {e}")
            return None


class SegmentedDownloader:
    """Multi-stream HTTP Range downloader."""

    def __init__(
        self,
        url: str,
        dest_path: str,
        headers: Optional[Dict[str, str]] = None,
        max_connections: int = 8,
        rate_limiter: Optional[Any] = None,
        on_progress: Optional[Callable[[int, int, float, int], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        is_paused: Optional[Callable[[], bool]] = None,
    ):
        self.url = url
        self.dest_path = dest_path
        self.headers = headers.copy() if headers else {}
        self.max_connections = max(1, min(max_connections, MAX_SEGMENTS))
        self.rate_limiter = rate_limiter
        self.on_progress = on_progress
        self.is_cancelled = is_cancelled or (lambda: False)
        self.is_paused = is_paused or (lambda: False)

        self.part_path = dest_path + ".part"
        self.meta_path = dest_path + ".part.meta.json"

        self.total_bytes: int = 0
        self.downloaded_bytes: int = 0
        self.supports_range: bool = False
        self.etag: str = ""
        self.last_modified: str = ""

        # Thread synchronization for random-access file writing
        self._file_lock = threading.Lock()
        self._file_handle = None

        # Speed and ETA calculation
        self._last_progress_time = 0.0
        self._last_downloaded_bytes = 0
        self._current_speed_bps = 0.0
        self._speed_samples: List[float] = []

    async def probe(self, client: httpx.AsyncClient) -> tuple[bool, int, str, str]:
        """Probe the server to determine range support, Content-Length, ETag, and Last-Modified."""
        probe_headers = self.headers.copy()
        probe_headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        probe_headers["Accept-Encoding"] = "identity"

        total_bytes = 0
        supports_range = False
        etag = ""
        last_modified = ""

        # Strategy 1: HEAD request
        try:
            resp = await client.head(self.url, headers=probe_headers, follow_redirects=True, timeout=15.0)
            if resp.status_code in (200, 206):
                etag = resp.headers.get("etag", "").strip('"')
                last_modified = resp.headers.get("last-modified", "")
                cr = resp.headers.get("content-range")
                if cr and "/" in cr:
                    try:
                        total_bytes = int(cr.split("/")[-1])
                    except (ValueError, TypeError):
                        pass
                if not total_bytes:
                    try:
                        total_bytes = int(resp.headers.get("content-length", 0))
                    except (ValueError, TypeError):
                        total_bytes = 0

                accept_ranges = resp.headers.get("accept-ranges", "").lower()
                if "bytes" in accept_ranges and total_bytes > 0:
                    supports_range = True
        except Exception as e:
            logger.debug(f"HEAD probe failed for {self.url}: {e}")

        # Strategy 2: If HEAD didn't confirm ranges or returned 0 length, try Range: bytes=0-0 GET probe
        if not supports_range or total_bytes <= 0:
            try:
                test_headers = probe_headers.copy()
                test_headers["Range"] = "bytes=0-0"
                resp = await client.get(self.url, headers=test_headers, follow_redirects=True, timeout=15.0)
                if resp.status_code == 206:
                    supports_range = True
                    cr = resp.headers.get("content-range", "")
                    if "/" in cr:
                        try:
                            total_bytes = int(cr.split("/")[-1])
                        except (ValueError, TypeError):
                            pass
                    etag = etag or resp.headers.get("etag", "").strip('"')
                    last_modified = last_modified or resp.headers.get("last-modified", "")
                elif resp.status_code == 200 and not total_bytes:
                    try:
                        total_bytes = int(resp.headers.get("content-length", 0))
                    except (ValueError, TypeError):
                        total_bytes = 0
            except Exception as e:
                logger.debug(f"Range probe GET failed for {self.url}: {e}")

        return supports_range, total_bytes, etag, last_modified

    def _calculate_segments(self, total_bytes: int) -> List[SegmentState]:
        """Dynamically determine optimal segment count and byte boundaries."""
        if total_bytes <= 0:
            return []

        # Determine segment count based on file size and max_connections
        if total_bytes < 2 * 1024 * 1024:         # < 2 MB
            num_segments = min(2, self.max_connections)
        elif total_bytes < 10 * 1024 * 1024:       # 2 MB - 10 MB
            num_segments = min(4, self.max_connections)
        elif total_bytes < 50 * 1024 * 1024:       # 10 MB - 50 MB
            num_segments = min(8, self.max_connections)
        else:                                      # > 50 MB
            num_segments = self.max_connections

        # Ensure segment size doesn't fall below MIN_SEGMENT_SIZE
        while num_segments > 1 and (total_bytes // num_segments) < MIN_SEGMENT_SIZE:
            num_segments //= 2

        num_segments = max(1, num_segments)
        seg_size = total_bytes // num_segments
        segments: List[SegmentState] = []

        start = 0
        for i in range(num_segments):
            if i == num_segments - 1:
                end = total_bytes - 1
            else:
                end = start + seg_size - 1
            segments.append(SegmentState(index=i, start_byte=start, end_byte=end))
            start = end + 1

        return segments

    def _sync_write(self, offset: int, chunk: bytes) -> None:
        """Write chunk at explicit offset in a thread-safe manner."""
        with self._file_lock:
            if self._file_handle and not self._file_handle.closed:
                self._file_handle.seek(offset)
                self._file_handle.write(chunk)

    async def _write_chunk(self, offset: int, chunk: bytes) -> None:
        """Asynchronously write chunk to disk via thread pool to keep event loop free."""
        await asyncio.to_thread(self._sync_write, offset, chunk)

    def _report_progress(self, force: bool = False) -> None:
        now = time.time()
        elapsed = now - self._last_progress_time
        if not force and elapsed < 0.2:
            return

        delta_bytes = self.downloaded_bytes - self._last_downloaded_bytes
        if elapsed > 0 and delta_bytes >= 0:
            instant_speed = delta_bytes / elapsed
            if self._current_speed_bps <= 0:
                self._current_speed_bps = instant_speed
            else:
                alpha = min(1.0, elapsed / 0.6)
                self._current_speed_bps = (alpha * instant_speed) + ((1.0 - alpha) * self._current_speed_bps)

        self._last_progress_time = now
        self._last_downloaded_bytes = self.downloaded_bytes

        eta = 0
        if self._current_speed_bps > 0 and self.total_bytes > self.downloaded_bytes:
            eta = int((self.total_bytes - self.downloaded_bytes) / self._current_speed_bps)

        if self.on_progress:
            try:
                self.on_progress(
                    self.downloaded_bytes,
                    self.total_bytes,
                    self._current_speed_bps,
                    eta,
                )
            except Exception as e:
                logger.debug(f"Progress callback error: {e}")

    async def _download_segment(
        self,
        client: httpx.AsyncClient,
        segment: SegmentState,
        meta: DownloadMetadata,
    ) -> bool:
        """Download an individual byte-range segment with retry and rate-limiting."""
        if segment.done or segment.remaining_bytes <= 0:
            segment.done = True
            return True

        retries = 0
        while retries <= MAX_WORKER_RETRIES:
            if self.is_cancelled() or self.is_paused():
                return False

            req_start = segment.current_offset
            req_end = segment.end_byte

            seg_headers = self.headers.copy()
            seg_headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
            seg_headers["Range"] = f"bytes={req_start}-{req_end}"
            seg_headers["Accept-Encoding"] = "identity"

            try:
                async with client.stream("GET", self.url, headers=seg_headers, timeout=30.0) as resp:
                    if resp.status_code not in (200, 206):
                        logger.debug(f"Segment {segment.index} HTTP error: {resp.status_code}")
                        if resp.status_code in (403, 404, 410):
                            return False
                        retries += 1
                        await asyncio.sleep(0.5 * (2 ** retries))
                        continue

                    # If server sent 200 instead of 206, it ignored our Range header!
                    if resp.status_code == 200 and segment.index > 0:
                        logger.warning("Server ignored Range header on non-first segment; falling back to single-stream.")
                        return False

                    async for chunk in resp.aiter_bytes(chunk_size=DEFAULT_CHUNK_SIZE):
                        if self.is_cancelled() or self.is_paused():
                            meta.segments = [asdict(s) for s in meta.segments]
                            meta.save(self.meta_path)
                            return False

                        chunk_len = len(chunk)
                        if chunk_len == 0:
                            continue

                        # Apply global cooperative rate limiter if configured
                        if self.rate_limiter:
                            await self.rate_limiter.acquire(chunk_len)

                        offset = segment.current_offset
                        await self._write_chunk(offset, chunk)

                        segment.downloaded_bytes += chunk_len
                        self.downloaded_bytes += chunk_len
                        self._report_progress()

                    if segment.current_offset > segment.end_byte:
                        segment.done = True
                        return True

            except (httpx.RequestError, httpx.TimeoutException, OSError) as exc:
                retries += 1
                logger.debug(f"Segment {segment.index} transient network error (attempt {retries}): {exc}")
                if retries > MAX_WORKER_RETRIES:
                    logger.warning(f"Segment {segment.index} failed after {MAX_WORKER_RETRIES} retries: {exc}")
                    return False
                await asyncio.sleep(min(4.0, 0.5 * (2 ** retries)))

        return segment.done

    async def _download_single_stream(
        self,
        client: httpx.AsyncClient,
        resume_offset: int = 0,
    ) -> bool:
        """Fallback single-stream chunked download with rate limiting and resume."""
        stream_headers = self.headers.copy()
        stream_headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        if resume_offset > 0:
            stream_headers["Range"] = f"bytes={resume_offset}-"

        retries = 0
        current_offset = resume_offset

        while retries <= MAX_WORKER_RETRIES:
            if self.is_cancelled() or self.is_paused():
                return False

            try:
                if current_offset > 0:
                    stream_headers["Range"] = f"bytes={current_offset}-"
                else:
                    stream_headers.pop("Range", None)

                async with client.stream("GET", self.url, headers=stream_headers, timeout=45.0) as resp:
                    if resp.status_code not in (200, 206):
                        if resp.status_code in (403, 404, 410):
                            return False
                        retries += 1
                        await asyncio.sleep(0.5 * (2 ** retries))
                        continue

                    # If resuming and server gave 200, it restarted from 0
                    if current_offset > 0 and resp.status_code == 200:
                        current_offset = 0
                        self.downloaded_bytes = 0
                        with self._file_lock:
                            if self._file_handle:
                                self._file_handle.seek(0)
                                self._file_handle.truncate(0)

                    if self.total_bytes <= 0:
                        cr = resp.headers.get("content-range")
                        if cr and "/" in cr:
                            try:
                                self.total_bytes = int(cr.split("/")[-1])
                            except (ValueError, TypeError):
                                pass
                        if not self.total_bytes:
                            try:
                                cl = int(resp.headers.get("content-length", 0))
                                self.total_bytes = (current_offset + cl) if cl > 0 else 0
                            except (ValueError, TypeError):
                                pass

                    async for chunk in resp.aiter_bytes(chunk_size=DEFAULT_CHUNK_SIZE):
                        if self.is_cancelled() or self.is_paused():
                            return False

                        chunk_len = len(chunk)
                        if chunk_len == 0:
                            continue

                        if self.rate_limiter:
                            await self.rate_limiter.acquire(chunk_len)

                        await self._write_chunk(current_offset, chunk)
                        current_offset += chunk_len
                        self.downloaded_bytes = current_offset
                        self._report_progress()

                    # Stream finished cleanly
                    return True

            except (httpx.RequestError, httpx.TimeoutException, OSError) as exc:
                retries += 1
                logger.debug(f"Single stream retry ({retries}/{MAX_WORKER_RETRIES}): {exc}")
                if retries > MAX_WORKER_RETRIES:
                    return False
                await asyncio.sleep(min(4.0, 0.5 * (2 ** retries)))

        return False

    async def download(self) -> bool:
        """Main entry point to execute segmented download with resume and fallback."""
        os.makedirs(os.path.dirname(os.path.abspath(self.dest_path)), exist_ok=True)
        self._last_progress_time = time.time()
        self._last_downloaded_bytes = 0

        limits = httpx.Limits(max_keepalive_connections=32, max_connections=64)
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0, limits=limits, verify=True) as client:
            # Step 1: Probe server for range support
            supports_range, total_bytes, etag, last_modified = await self.probe(client)
            self.supports_range = supports_range
            self.total_bytes = total_bytes
            self.etag = etag
            self.last_modified = last_modified

            # Step 2: Check for existing resume metadata
            metadata = DownloadMetadata.load(self.meta_path)
            segments: List[SegmentState] = []
            resumed = False

            if (
                metadata
                and self.supports_range
                and metadata.total_bytes == self.total_bytes
                and os.path.isfile(self.part_path)
            ):
                # Verify metadata consistency
                valid = True
                loaded_segments = []
                total_dl = 0
                for s_dict in metadata.segments:
                    try:
                        s = SegmentState(**s_dict)
                        loaded_segments.append(s)
                        total_dl += s.downloaded_bytes
                    except Exception:
                        valid = False
                        break
                if valid and loaded_segments:
                    segments = loaded_segments
                    self.downloaded_bytes = total_dl
                    resumed = True
                    logger.info(f"Resuming download with {len(segments)} segments at {self.downloaded_bytes}/{self.total_bytes} bytes")

            # Step 3: Initialize new download segments if not resuming
            if not resumed:
                if self.supports_range and self.total_bytes > 0:
                    segments = self._calculate_segments(self.total_bytes)
                else:
                    segments = []

            # Step 4: Open/pre-allocate destination file
            file_mode = "r+b" if os.path.isfile(self.part_path) else "w+b"
            self._file_handle = open(self.part_path, file_mode)

            try:
                # Pre-allocate if new segmented download
                if not resumed and self.total_bytes > 0 and self.supports_range:
                    # Sparse / fast allocation: write 0 at end of file
                    self._file_handle.seek(self.total_bytes - 1)
                    self._file_handle.write(b"\0")
                    self._file_handle.flush()

                # Step 5: Execute segmented download if supported
                if self.supports_range and segments and len(segments) > 1:
                    meta = DownloadMetadata(
                        url=self.url,
                        total_bytes=self.total_bytes,
                        etag=self.etag,
                        last_modified=self.last_modified,
                        segments=[asdict(s) for s in segments],
                    )
                    meta.save(self.meta_path)

                    # Run worker tasks concurrently
                    tasks = [
                        asyncio.create_task(self._download_segment(client, seg, meta))
                        for seg in segments
                    ]

                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    all_success = all(r is True for r in results)

                    if self.is_cancelled():
                        self._cleanup()
                        return False

                    if self.is_paused():
                        meta.segments = [asdict(s) for s in segments]
                        meta.save(self.meta_path)
                        return False

                    if all_success:
                        self._finish()
                        return True
                    else:
                        logger.warning("Segmented download failed or stalled; falling back to single-stream.")

                # Step 6: Fallback or single-stream download
                existing_bytes = os.path.getsize(self.part_path) if os.path.isfile(self.part_path) else 0
                resume_offset = existing_bytes if (resumed and existing_bytes < self.total_bytes) else 0

                success = await self._download_single_stream(client, resume_offset=resume_offset)

                if self.is_cancelled():
                    self._cleanup()
                    return False

                if self.is_paused():
                    return False

                if success:
                    self._finish()
                    return True
                else:
                    return False

            finally:
                with self._file_lock:
                    if self._file_handle and not self._file_handle.closed:
                        self._file_handle.close()
                        self._file_handle = None

    def _finish(self) -> None:
        """Finalize download: flush, close, and atomically rename .part to final destination."""
        with self._file_lock:
            if self._file_handle and not self._file_handle.closed:
                self._file_handle.flush()
                self._file_handle.close()
                self._file_handle = None

        # Clean up metadata file
        if os.path.isfile(self.meta_path):
            try:
                os.remove(self.meta_path)
            except OSError:
                pass

        # Atomically rename .part to final file
        if os.path.isfile(self.part_path):
            if os.path.exists(self.dest_path):
                try:
                    os.remove(self.dest_path)
                except OSError:
                    pass
            os.rename(self.part_path, self.dest_path)

        if os.path.isfile(self.dest_path):
            self.downloaded_bytes = os.path.getsize(self.dest_path)
            if self.total_bytes <= 0:
                self.total_bytes = self.downloaded_bytes
            self._report_progress(force=True)

    def _cleanup(self) -> None:
        """Remove .part and metadata files on explicit user cancellation."""
        with self._file_lock:
            if self._file_handle and not self._file_handle.closed:
                self._file_handle.close()
                self._file_handle = None

        for path in (self.part_path, self.meta_path):
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

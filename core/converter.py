"""Media Format Converter for Savitar.

Converts downloaded video and audio files into various formats (MP3, MP4, M4A, WebM, AVI, MKV, WAV, FLAC)
using local FFmpeg.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable, Optional

from .binaries import ffmpeg_path, popen_kwargs
from .updater import ensure_ffmpeg

logger = logging.getLogger(__name__)

AUDIO_FORMATS = {"mp3", "m4a", "wav", "aac", "flac", "ogg", "opus"}
VIDEO_FORMATS = {"mp4", "mkv", "webm", "avi", "mov", "flv"}


def build_ffmpeg_args(source: str, target_ext: str, output_path: str) -> list[str]:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("ffmpeg binary not found. Please check Settings > Engine.")
    target = target_ext.lower().lstrip(".")

    args = [ffmpeg, "-y", "-i", source]

    if target == "mp3":
        args += ["-vn", "-acodec", "libmp3lame", "-q:a", "0"]
    elif target == "m4a":
        args += ["-vn", "-acodec", "aac", "-b:a", "256k"]
    elif target == "wav":
        args += ["-vn", "-acodec", "pcm_s16le"]
    elif target == "aac":
        args += ["-vn", "-acodec", "aac", "-b:a", "256k"]
    elif target == "flac":
        args += ["-vn", "-acodec", "flac"]
    elif target in ("ogg", "opus"):
        args += ["-vn", "-acodec", "libopus", "-b:a", "192k"]
    elif target == "mp4":
        args += [
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "22",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
        ]
    elif target == "webm":
        args += [
            "-c:v", "libvpx-vp9",
            "-crf", "30",
            "-b:v", "0",
            "-c:a", "libopus",
            "-b:a", "128k",
        ]
    elif target == "mkv":
        args += ["-c:v", "libx264", "-c:a", "aac"]
    elif target == "avi":
        args += ["-c:v", "mpeg4", "-qscale:v", "3", "-c:a", "libmp3lame", "-qscale:a", "2"]
    elif target == "mov":
        args += ["-c:v", "libx264", "-c:a", "aac"]
    else:
        args += []

    args.append(output_path)
    return args


async def convert_file(
    source_path: str,
    target_format: str,
    output_dir: Optional[str] = None,
) -> dict:
    """Convert source media file to the target format."""
    if not source_path or not os.path.isfile(source_path):
        return {"ok": False, "message": "Source file does not exist on disk."}

    # Ensure ffmpeg binary is available
    if not ffmpeg_path():
        try:
            await ensure_ffmpeg()
        except Exception:
            pass
        if not ffmpeg_path():
            return {
                "ok": False,
                "message": "Conversion engine is required. Please check Settings > Engine.",
            }

    target_ext = target_format.lower().lstrip(".")
    src = Path(source_path)

    # Output directory: {Downloads}/Savitar/Converted
    if not output_dir:
        base = os.path.join(os.path.expanduser("~"), "Downloads", "Savitar", "Converted")
        os.makedirs(base, exist_ok=True)
        out_dir = Path(base)
    else:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

    out_filename = f"{src.stem}.{target_ext}"
    out_path = out_dir / out_filename

    # Avoid collision if source is the exact same file
    if out_path.resolve() == src.resolve():
        out_path = out_dir / f"{src.stem}_converted.{target_ext}"

    cmd = build_ffmpeg_args(str(src), target_ext, str(out_path))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **popen_kwargs(),
        )
        _, stderr = await proc.communicate()

        if proc.returncode == 0 and out_path.is_file():
            size = out_path.stat().st_size
            return {
                "ok": True,
                "output_path": str(out_path),
                "filename": out_path.name,
                "size_bytes": size,
                "target_format": target_ext,
                "message": f"Successfully converted to {target_ext.upper()}",
            }
        else:
            err_msg = stderr.decode("utf-8", errors="replace")[-500:] if stderr else "Conversion failed"
            return {"ok": False, "message": f"FFmpeg error: {err_msg}"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}

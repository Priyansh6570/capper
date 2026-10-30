"""Cutting a video that is longer than YouTube allows into parts."""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

import gpu
from config import NO_CONSOLE

MAX_PART_S = 11 * 3600 + 55 * 60   # YouTube's limit is 12 hours; the margin covers cutting at keyframes


def part_count(duration_s: float) -> int:
    return max(1, math.ceil(duration_s / MAX_PART_S))


def split(source: Path, folder: Path, parts: int, duration_s: float) -> list[Path]:
    """Cut `source` into `parts` pieces of equal length without re-encoding; returns them in order."""
    folder.mkdir(parents=True, exist_ok=True)
    cuts = ",".join(f"{duration_s * i / parts:.3f}" for i in range(1, parts))
    done = subprocess.run(
        [gpu.ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-c", "copy", "-f", "segment",
         "-segment_times", cuts, "-reset_timestamps", "1", "-segment_start_number", "1",
         "-segment_format_options", "movflags=+faststart", str(folder / "part_%02d.mp4")],
        capture_output=True, text=True, **NO_CONSOLE)
    found = sorted(folder.glob("part_*.mp4"))
    if done.returncode or len(found) != parts:
        raise RuntimeError(f"Could not split the video into {parts} parts: {done.stderr.strip()[-300:]}")
    return found

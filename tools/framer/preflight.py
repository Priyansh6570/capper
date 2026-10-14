"""Checks run before a batch starts, so a problem shows up now instead of an hour into the run.

Each check returns {name, status, detail} with status "ok", "warn" (the batch can run, but
something will be slower or will pause) or "fail" (the batch cannot do its job).
"""
from __future__ import annotations

import ctypes
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import claude_cli
import claude_usage
import projects
import sites
from config import NO_CONSOLE, PROJECTS_DIR
from gpu import ffmpeg_path, nvidia_gpu

DISK_PER_CHAPTER_GB = 3
OTHER_GPU_APPS_WARN_MB = 1500       # Chatterbox needs about 3 GB of a 4 GB card
AC_OFFLINE = 0


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def _result(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def _on_battery() -> bool:
    if sys.platform != "win32":
        return False
    power = _PowerStatus()
    return bool(ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power))) and power.ACLineStatus == AC_OFFLINE


def _gpu_used_mb() -> int | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=15, **NO_CONSOLE).stdout
        return int(out.splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _claude() -> dict:
    status = claude_cli.cli_status()
    return _result("Claude Code", "ok" if status["ready"] else "fail", status["message"])


def _source(project: dict, numbers: list[int]) -> dict:
    missing = [n for n in numbers if not any((projects.chapter_work_dir(project["slug"], n) / "download").glob("*.pdf"))]
    if not missing:
        return _result("Chapter source", "ok", "Every selected chapter is already downloaded.")
    if sites.detect(project["url"] or "") is None:
        return _result("Chapter source", "fail", f"{len(missing)} chapter(s) need downloading but the project has no "
                       "supported series URL (webtoons.com or kingofshojo.com).")
    return _result("Chapter source", "ok", f"{len(missing)} chapter(s) will be downloaded from {project['url']}.")


def _ffmpeg() -> dict:
    ffmpeg = ffmpeg_path()
    if not (Path(ffmpeg).exists() or shutil.which(ffmpeg)):
        return _result("Video encoder", "fail", "ffmpeg was not found. Run setup.bat again.")
    return _result("Video encoder", "ok", "ffmpeg found (libx264 encoding; required, there is no fallback renderer).")


def _disk(numbers: list[int]) -> dict:
    free_gb = shutil.disk_usage(PROJECTS_DIR).free / 1e9
    need = DISK_PER_CHAPTER_GB * len(numbers)
    if free_gb < 1:
        return _result("Disk space", "fail", f"Only {free_gb:.1f} GB free.")
    status = "ok" if free_gb >= need else "warn"
    return _result("Disk space", status, f"{free_gb:.0f} GB free; about {need} GB needed for {len(numbers)} chapter(s).")


def _voice(settings: dict) -> dict:
    engine = settings["voice"]["engine"]
    package = "kokoro" if engine == "kokoro" else "chatterbox"
    if importlib.util.find_spec(package) is None:
        return _result("Narration engine", "fail", f"{engine} is not installed. Run setup.bat again.")
    return _result("Narration engine", "ok", f"{engine.capitalize()} is installed.")


def _gpu(settings: dict) -> dict:
    if nvidia_gpu() is None:
        return _result("GPU", "warn", "No NVIDIA GPU found: narration will run on the CPU (much slower).")
    used = _gpu_used_mb()
    if settings["voice"]["engine"] == "chatterbox" and used and used > OTHER_GPU_APPS_WARN_MB:
        return _result("GPU", "warn", f"{used / 1000:.1f} GB of GPU memory is already in use by other apps; "
                       "close them if narration spills or slows down.")
    return _result("GPU", "ok", "The GPU is available.")


def _power() -> dict:
    if _on_battery():
        return _result("Power", "warn", "Running on battery: the GPU is power-capped, so narration and video "
                       "render roughly twice as slowly. Plug in.")
    return _result("Power", "ok", "Plugged in.")


def _usage(limits: dict) -> dict:
    snapshot = claude_usage.snapshot()
    parts, reached = [], False
    for window, key in (("five_hour", "five_hour_pct"), ("seven_day", "seven_day_pct")):
        if window in snapshot:
            parts.append(f"{window.replace('_', '-')} {snapshot[window]['percent']:.0f}% (limit {limits[key]}%)")
            reached = reached or snapshot[window]["percent"] >= limits[key]
    if reached:
        return _result("Claude usage", "warn", "; ".join(parts) + ". A limit is already reached, so Claude work "
                       "will wait for the reset.")
    return _result("Claude usage", "ok", "; ".join(parts) or "No usage reading yet; it is read on the first call.")


def check(project: dict, numbers: list[int], steps: set[str]) -> list[dict]:
    """The checks that matter for the `steps` the batch will run."""
    settings = project["settings"]
    checks = [_source(project, numbers)] if "download" in steps else []
    if "script" in steps:
        checks = [_claude(), *checks]
    if steps & {"audio", "video"}:
        checks.append(_ffmpeg())
    checks.append(_disk(numbers))
    if "audio" in steps:
        checks += [_voice(settings), _gpu(settings)]
    if steps & {"audio", "video"}:
        checks.append(_power())
    if "script" in steps:
        checks.append(_usage(settings["batch"]))
    return checks

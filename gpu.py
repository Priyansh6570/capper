"""GPU detection shared by stage 6 (TTS), stage 7 (video), setup.bat and the framer's
GPU status card. Probes run in subprocesses so no process imports torch just to ask.

`python gpu.py nvenc` exits 0 when h264_nvenc can really encode (setup.bat's check).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from atomic_io import atomic_write_json, read_json_with_backup_fallback
from config import NO_CONSOLE, ROOT

USAGE_FILE = ROOT / "gpu_usage.json"
VENDOR_FFMPEG = ROOT / "vendor" / "ffmpeg" / "bin" / "ffmpeg.exe"

_TORCH_PROBE = (
    "import json, torch;"
    "print(json.dumps({'version': torch.__version__, 'cuda_build': torch.version.cuda,"
    " 'available': torch.cuda.is_available()}))"
)


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **NO_CONSOLE)
    except (OSError, subprocess.SubprocessError):
        return None


def nvidia_gpu() -> dict | None:
    """Name, VRAM (MB) and driver of the first NVIDIA GPU, or None without a working driver."""
    proc = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                 "--format=csv,noheader,nounits"], 15)
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return None
    name, vram, driver = [p.strip() for p in proc.stdout.splitlines()[0].split(",")]
    return {"name": name, "vram_mb": int(vram), "driver": driver}


def torch_info() -> dict | None:
    """torch's version, CUDA build and cuda.is_available() as seen by this interpreter."""
    proc = _run([sys.executable, "-c", _TORCH_PROBE], 60)
    if proc is None or proc.returncode != 0:
        return None
    return json.loads(proc.stdout.strip().splitlines()[-1])


def ffmpeg_path() -> str:
    """The app's own ffmpeg when setup downloaded one, else whatever is on PATH."""
    if VENDOR_FFMPEG.exists():
        return str(VENDOR_FFMPEG)
    return shutil.which("ffmpeg") or "ffmpeg"


def nvenc_works(ffmpeg: str) -> bool:
    """True only if a real h264_nvenc encode succeeds - the encoder being listed is not
    enough, it is listed on machines with no NVIDIA GPU or driver too."""
    proc = _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc",
                 "-f", "null", "-"], 30)
    return proc is not None and proc.returncode == 0


def record_usage(stage: str, backend: str, on_gpu: bool) -> None:
    """Remember what the last audio/video run actually used, for the status card."""
    usage = read_usage()
    usage[stage] = {"backend": backend, "gpu": on_gpu, "at": time.strftime("%Y-%m-%d %H:%M")}
    atomic_write_json(USAGE_FILE, usage, backup=False)


def read_usage() -> dict:
    try:
        return read_json_with_backup_fallback(USAGE_FILE)
    except (OSError, ValueError):
        return {}


def _audio_verdict(gpu: dict | None, torch: dict | None) -> tuple[bool, str]:
    if torch is None:
        return False, "PyTorch is not installed."
    if torch["available"]:
        return True, f"PyTorch {torch['version']} sees the GPU."
    if gpu is None:
        return False, "No NVIDIA GPU or driver detected."
    if torch["cuda_build"] is None:
        return False, f"PyTorch {torch['version']} is the CPU-only build. Re-run setup.bat to install the CUDA build."
    return False, (f"PyTorch has CUDA support but cannot reach the GPU. "
                   f"Update the NVIDIA driver (installed: {gpu['driver']}).")


def status() -> dict:
    gpu = nvidia_gpu()
    torch = torch_info()
    ffmpeg = ffmpeg_path()
    audio_ok, audio_note = _audio_verdict(gpu, torch)
    nvenc = gpu is not None and nvenc_works(ffmpeg)
    return {
        "gpu": gpu,
        "audio": {"gpu_ready": audio_ok, "note": audio_note},
        "video": {"gpu_ready": nvenc, "ffmpeg": ffmpeg,
                  "note": "h264_nvenc encodes on the GPU." if nvenc else
                          "Video will encode on the CPU (libx264)."},
        "last_run": read_usage(),
    }


if __name__ == "__main__":
    if sys.argv[1:] == ["nvenc"]:
        sys.exit(0 if nvenc_works(ffmpeg_path()) else 1)
    print(json.dumps(status(), indent=2))

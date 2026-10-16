"""Optional feature packages, dependency checks and installs."""
from __future__ import annotations

import json
import subprocess
import sys

import gpu
import preflight
from config import NO_CONSOLE, ROOT


# --------------------------------------------------------------------------- #
# Dependency self-repair - pulling new CODE (below) can't install a new
# PACKAGE that code needs, and .venv is deliberately untouched by git. deps.py
# compares every requirements file against what the venv really has and runs
# the same CUDA-torch-safe install that setup.bat uses. It runs as a subprocess,
# never imported, so a pull that changes deps.py itself takes effect at once.
# System tools pip can't install (ffmpeg, git, Claude Code) are reported by
# preflight.system_tools().
# --------------------------------------------------------------------------- #
FEATURE_PACKAGES = {
    "flask": "the app itself",
    "PyMuPDF": "PDF page rendering",
    "Pillow": "image processing (compositing, previews, thumbnails)",
    "requests": "chapter metadata lookup",
    "beautifulsoup4": "chapter metadata lookup",
    "pystray": "the system tray icon",
    "python-dotenv": "reading saved API keys from .env",
    "opencv-python-headless": "speech-bubble detection (Claude script engine)",
    "torch": "AI narration + GPU acceleration",
    "torchaudio": "AI narration + GPU acceleration",
    "chatterbox-tts": "AI narration (Chatterbox voice)",
    "kokoro": "AI narration (Kokoro voice)",
    "soundfile": "voice reference processing",
    "numpy": "audio processing",
}


def _deps_cli(*args: str) -> list[str]:
    return [sys.executable, str(ROOT / "deps.py"), *args]


def _missing_packages() -> list[dict]:
    out = subprocess.run(_deps_cli("check", "--json"), cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8", timeout=120, **NO_CONSOLE).stdout
    return [{"package": n, "feature": FEATURE_PACKAGES.get(n, "part of the app")}
            for n in json.loads(out.strip().splitlines()[-1])]


def system_tool_problems() -> list[dict]:
    return [tool for tool in preflight.system_tools() if tool["status"] != "ok"]


def install_deps_stream():
    proc = subprocess.Popen(_deps_cli("install"), cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, bufsize=1,
                            encoding="utf-8", errors="replace", **NO_CONSOLE)
    for line in proc.stdout:
        yield {"type": "log", "line": line.rstrip("\n")}
    if proc.wait() != 0:
        yield {"type": "error", "message": "Installing the packages failed - see the log above."}
        return
    cuda_ok = (gpu.torch_info() or {}).get("available") if gpu.nvidia_gpu() else None
    yield {"type": "result", "missing": _missing_packages(), "cuda_ok": cuda_ok}

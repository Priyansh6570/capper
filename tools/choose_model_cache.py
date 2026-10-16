"""Choose where this PC keeps the Hugging Face model cache; setup.bat runs it before the voice model download.

The voice model is several GB, so it goes on a fixed drive other than Windows' own when this PC has one
with room (D:\\hf_cache), and stays in the Windows default otherwise. The choice is saved as `hf_home` in
app_settings.json, which config.py hands to huggingface_hub as HF_HOME. Nothing is chosen when the PC
already has a choice: an HF_HOME in the environment, or a saved setting.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools" / "framer")]

MIN_FREE_GB = 20
DRIVE_FIXED = 3     # GetDriveTypeW: a local disk, not removable, network or optical


def fixed_drives() -> list[Path]:
    present = ctypes.windll.kernel32.GetLogicalDrives()
    drives = [Path(f"{chr(ord('A') + i)}:\\") for i in range(26) if present >> i & 1]
    return [d for d in drives if ctypes.windll.kernel32.GetDriveTypeW(str(d)) == DRIVE_FIXED]


def choose(drives: list[Path], system_drive: Path, free_gb: Callable[[Path], float]) -> Path | None:
    """hf_cache on the first fixed drive that is not the system drive and has MIN_FREE_GB free."""
    for drive in sorted(drives):
        if drive != system_drive and free_gb(drive) >= MIN_FREE_GB:
            return drive / "hf_cache"
    return None


def main() -> int:
    import app_settings

    if os.environ.get("HF_HOME") or app_settings.model_cache():
        print(f"Model cache: keeping the existing choice ({os.environ.get('HF_HOME') or app_settings.model_cache()}).")
        return 0
    system_drive = Path(os.environ.get("SystemDrive", "C:") + "\\")
    chosen = choose(fixed_drives(), system_drive, lambda drive: shutil.disk_usage(drive).free / 1e9)
    if chosen is None:
        print("Model cache: using the Windows default (no other fixed drive with enough room).")
        return 0
    app_settings.set_model_cache(str(chosen))
    print(f"Model cache: {chosen} (saved in app_settings.json; change it on the Projects screen).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

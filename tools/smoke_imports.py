"""Imports every framer module, every pipeline stage and the engine libraries the stages load lazily.

Exit 0 when all of them import, else 1 after printing every failure. setup.bat runs it as its
final check, so an install that would not launch is reported at install time, not at first start.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools" / "framer")]

ROOT_MODULES = ("orchestrator", "llm", "deps")
SCRIPTS = {"tray_launcher"}     # exits the process when its own imports fail, so it is tested by running it
ENGINE_MODULES = (
    "chatterbox.tts", "chatterbox.tts_turbo", "kokoro", "espeakng_loader",
    "phonemizer.backend.espeak.wrapper", "torchaudio.functional", "edge_tts", "mutagen.mp3",
    "huggingface_hub",
)


def modules() -> list[str]:
    """Every module of the framer is listed, not just app.py: some are only reached lazily at runtime."""
    framer = [m.name for m in pkgutil.walk_packages([str(ROOT / "tools" / "framer")]) if m.name not in SCRIPTS]
    stages = [f"stages.{m.name}" for m in pkgutil.iter_modules([str(ROOT / "stages")])]
    return [*framer, *ROOT_MODULES, *stages, *ENGINE_MODULES]


def main() -> int:
    failures = []
    for name in modules():
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - report every broken import, not just the first
            failures.append(name)
            print(f"[FAIL] {name}\n{traceback.format_exc()}")
    print(f"{len(failures)} of {len(modules())} modules failed to import." if failures
          else "All modules import.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

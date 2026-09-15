"""Central configuration. Everything is local and file-based by design."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INPUTS_DIR = ROOT / "inputs"     # source PDFs go here
WORK_DIR = ROOT / "work"         # per-chapter intermediate files
OUTPUT_DIR = ROOT / "output"     # final artifacts (storyboard / video)

# Stage 1 — page render resolution. Higher = sharper but bigger files.
PAGE_DPI = 150

# Stage 6 (stub) — fake-audio timing so downstream slide-timing logic is real.
TTS_SECONDS_PER_CHAR = 0.06
TTS_MIN_SECONDS = 1.5
TTS_SAMPLE_RATE = 22050

# Stage 6 (real) — edge-tts neural voiceover. Voice is a Microsoft neural voice
# short name; see `edge-tts --list-voices`. Default: natural male US narrator.
TTS_VOICE = "en-US-GuyNeural"


def chapter_work_dir(chapter_id: str) -> Path:
    return WORK_DIR / chapter_id


def chapter_output_dir(chapter_id: str) -> Path:
    return OUTPUT_DIR / chapter_id

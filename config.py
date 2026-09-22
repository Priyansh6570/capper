"""Central configuration. Everything is local and file-based by design."""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INPUTS_DIR = ROOT / "inputs"     # source PDFs go here
WORK_DIR = ROOT / "work"         # per-chapter intermediate files
OUTPUT_DIR = ROOT / "output"     # final artifacts (storyboard / video)
PROJECTS_DIR = ROOT / "projects"  # framer project system (tools/framer/projects.py)

# --------------------------------------------------------------------------- #
# Chapter-folder redirect for the framer's project system.
#
# Every stage resolves its folder via chapter_work_dir()/chapter_output_dir()
# below, imported as bare functions (`from config import chapter_work_dir`), so
# the redirect must live INSIDE these function bodies - and must keep reading
# the module-global WORK_DIR/OUTPUT_DIR at call time, not a captured default,
# because stages/s2_detect_panels.py's self-test monkeypatches config.WORK_DIR
# directly and must keep working.
#
# Two ways a redirect gets installed:
#   - `chapter_scope()` sets a ContextVar - used by the Flask app (in-process,
#     one project+chapter per request).
#   - MANHWA_WORK_ROOT / MANHWA_OUTPUT_ROOT env vars - used by a subprocess
#     (the orchestrator spawned by /generate_stream), read once at import.
#
# Either way, chapter_id is deliberately IGNORED once a redirect is active:
# only one chapter is ever in scope at a time, and the folder already encodes
# project+chapter. With no scope and no env vars, behavior is unchanged, so
# CLI runs over legacy work/<chapter>/ folders are unaffected.
# --------------------------------------------------------------------------- #
_ENV_WORK_ROOT = os.environ.get("MANHWA_WORK_ROOT")
_ENV_OUTPUT_ROOT = os.environ.get("MANHWA_OUTPUT_ROOT")

_scope: ContextVar[tuple[Path, Path] | None] = ContextVar("chapter_scope", default=None)


def set_chapter_scope(work_dir, output_dir) -> None:
    _scope.set((Path(work_dir), Path(output_dir)))


def clear_chapter_scope() -> None:
    _scope.set(None)


@contextmanager
def chapter_scope(work_dir, output_dir):
    """Redirect chapter_work_dir()/chapter_output_dir() to fixed folders for the
    duration of the block, regardless of the chapter_id passed to them."""
    token = _scope.set((Path(work_dir), Path(output_dir)))
    try:
        yield
    finally:
        _scope.reset(token)

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
    scoped = _scope.get()
    if scoped is not None:
        return scoped[0]
    if _ENV_WORK_ROOT:
        return Path(_ENV_WORK_ROOT)
    return WORK_DIR / chapter_id


def chapter_output_dir(chapter_id: str) -> Path:
    scoped = _scope.get()
    if scoped is not None:
        return scoped[1]
    if _ENV_OUTPUT_ROOT:
        return Path(_ENV_OUTPUT_ROOT)
    return OUTPUT_DIR / chapter_id

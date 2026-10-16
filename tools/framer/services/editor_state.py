"""The editor's saved state for a chapter, and the narration it is pre-filled from."""
from __future__ import annotations

from pathlib import Path

import claude_engine
import config
import projects
from atomic_io import read_json_with_backup_fallback
from config import chapter_work_dir
from manifest import Manifest, Panel
from services.chapter_files import _framer_dir


# --------------------------------------------------------------------------- #
# Claude script engine - the local `claude` CLI instead of the Gemini API (see
# claude_engine.py). A "script" job splits the chapter strip into panels, runs
# the two passes, and leaves the narration blocks in work/<ch>/claude/state.json;
# the framer then opens the chapter with every block boxed to its panels (see
# state_from_narration). TTS and render consume the framer's export unchanged.
# --------------------------------------------------------------------------- #
def claude_state_path(chapter_id: str) -> Path:
    return chapter_work_dir(chapter_id) / "claude" / "state.json"


def manifest_panels(chapter_id: str) -> list[Panel]:
    work = chapter_work_dir(chapter_id)
    if not (work / "manifest.json").exists():
        return []
    pages = Manifest.load(work).pages
    return pages[0].panels if pages else []


def narration_estimate(slug: str, n: int, chars_per_sec: float) -> dict | None:
    """Words and estimated spoken length (seconds, at the project's speaking pace) of chapter
    `n`'s current script, or None when it has none yet."""
    with config.chapter_scope(projects.chapter_work_path(slug, n), projects.chapter_output_path(slug, n)):
        state = resolve_editor_state(f"{slug}_ch{n}")
    texts = [text for line in (state or {}).get("lines", []) if (text := line.get("text", "").strip())]
    if not texts:
        return None
    return {"words": sum(len(t.split()) for t in texts), "seconds": round(sum(map(len, texts)) / chars_per_sec)}


def state_from_narration(chapter_id: str) -> dict | None:
    """Editor lines for a chapter's Claude narration: one line per block, with the
    block's frames cropped and flopped as the engine decided."""
    narration = claude_engine.read_narration(claude_state_path(chapter_id))
    if not narration:
        return None
    boxes = {p.id: p.bbox for p in manifest_panels(chapter_id)}

    def frames_of(block: dict) -> list[dict]:
        if "frames" in block:
            return [{"bbox": f["bbox"], "flip": f["flip"]} for f in block["frames"]]
        return [{"bbox": boxes[i], "flip": False} for i in block["panel_ids"] if i in boxes]

    return {
        "chapter_id": chapter_id,
        "saved_at": narration["saved_at"],
        "lines": [{"text": block["narration"], "duration_s": block["duration_s"],
                   "mode": block.get("mode", "sequence"), "pace": block.get("pace"), "frames": frames_of(block)}
                  for block in narration["blocks"]],
    }


# --------------------------------------------------------------------------- #
# editor state (per-chapter framer working state) + autosave recovery.
#
# NOTE: this is the FRAMER's own editing session (lines/frames/blur/etc.) - a
# different concept from the "project" system in projects.py (a whole manhwa
# series with a chapter list). Named /editor_state/* to avoid colliding with
# the new /api/projects/* endpoints.
# --------------------------------------------------------------------------- #
def editor_state_path(chapter_id: str, recovery: bool) -> Path:
    return _framer_dir(chapter_id) / ("recovery.json" if recovery else "editor_state.json")


def read_editor_state(path: Path) -> dict | None:
    """The saved state at `path`, or None if absent/unreadable. Falls back to the
    .bak of the same file if the primary is corrupt - see atomic_io.py."""
    if not path.exists() and not path.with_name(path.name + ".bak").exists():
        return None
    try:
        state = read_json_with_backup_fallback(path)
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _state_from_mapping(chapter_id: str) -> dict | None:
    """Rebuild editor lines from the exported mapping.json - the on-disk record
    of finished boxing work, used when neither editor save file has any."""
    mapping = read_editor_state(_framer_dir(chapter_id) / "mapping.json")
    if not mapping or not mapping.get("lines"):
        return None
    return {
        "chapter_id": chapter_id,
        "lines": [{
            "text": ln.get("text", ""),
            "duration_s": ln.get("duration_s", 4),
            "mode": ln.get("mode", "sequence"),
            "pace": ln.get("pace"),
            "frames": [{"bbox": f["bbox"], "flip": f.get("flip", False),
                        "blur": f.get("blur_regions", [])} for f in ln.get("frames", [])],
        } for ln in mapping["lines"]],
    }


def resolve_editor_state(chapter_id: str) -> dict | None:
    """The chapter's editor state, derived from whatever is on disk: the most
    recently written of editor_state.json / recovery.json / the Claude narration
    pre-fill that actually holds lines, else the exported mapping.json, else any
    (empty) save. Never trusts
    one file just because it exists - an empty or stale one must not shadow
    real work in another."""
    saved = [s for s in (read_editor_state(editor_state_path(chapter_id, r)) for r in (False, True)) if s]
    prefilled = state_from_narration(chapter_id)
    populated = sorted((s for s in [*saved, prefilled] if s and s.get("lines")),
                       key=lambda s: s.get("saved_at", ""), reverse=True)
    if populated:
        return populated[0]
    return _state_from_mapping(chapter_id) or (saved[0] if saved else None)

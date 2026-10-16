"""The manifest is the spine of the pipeline.

Each stage reads it, does its work on disk, adds its fields and advances `stage`. Nothing is passed
in memory between stages - only files and this JSON, which keeps every stage independently testable.

The editor's export writes the manifest (the stitched strip as the only Page, one beat per narration
line) and leaves it at the "frames" marker; the orchestrator then runs the stages after it.

Field ownership:
    s1      -> Page.image (the stitched strip)
    export  -> Page.panels and Manifest.beats (crop, script_line, emotion = the line's pace, duration_s)
    s6      -> Panel.audio, Panel.duration_s
    s7      -> reads the beats and renders the video

Manifests written before the terminal pipeline was removed carry fields and stage markers that no
longer exist; loading ignores the fields and maps the markers (see LEGACY_STAGES).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import List, Optional

from atomic_io import atomic_write_json, read_json_with_backup_fallback

# Ordered progress markers. Each stage SETS the next marker when it finishes.
# Keep in sync with orchestrator.STAGES.
STAGE_ORDER = ["init", "pages", "frames", "audio", "video"]

# Markers of the removed stages, mapped to where a manifest that carried them stands now.
LEGACY_STAGES = {"panels": "pages", "vision": "pages", "script": "pages", "match": "pages",
                 "context": "pages", "clean": "frames"}


def _known(cls, data: dict) -> dict:
    """`data` without the keys `cls` has no field for (written by an older version)."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


@dataclass
class Panel:
    id: str
    bbox: List[float]                   # [x, y, w, h] in strip pixels
    order: int                          # global reading order across the chapter
    crop: Optional[str] = None          # the frame image shown for this panel / beat
    emotion: Optional[str] = None       # the narration line's pace (see pacing.py)
    script_line: Optional[str] = None   # narration line
    audio: Optional[str] = None         # voiceover clip path           (s6)
    duration_s: Optional[float] = None  # clip length -> slide timing   (s6)


@dataclass
class Page:
    index: int                          # 1-based page number
    image: str                          # rendered page image            (s1)
    panels: List[Panel] = field(default_factory=list)


@dataclass
class Manifest:
    chapter_id: str
    source_pdf: str
    work_dir: str
    stage: str = "init"
    doc_type: str = "manga"             # "manga" (page-based) or "webtoon" (strip)
    pages: List[Page] = field(default_factory=list)
    beats: List[Panel] = field(default_factory=list)  # one per narration line

    @property
    def path(self) -> Path:
        return Path(self.work_dir) / "manifest.json"

    def save(self) -> None:
        # Atomic (temp file + os.replace, never a half-written manifest.json)
        # plus a manifest.json.bak of whatever this save is about to overwrite
        # - see atomic_io.py. This file is the whole pipeline's boxing/script
        # state (beats, panels, script_line, ...), saved after every stage.
        Path(self.work_dir).mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, asdict(self))

    @classmethod
    def load(cls, work_dir: str | Path) -> "Manifest":
        # Falls back to manifest.json.bak if the primary file is missing,
        # empty, or unparsable - see atomic_io.py.
        data = read_json_with_backup_fallback(Path(work_dir) / "manifest.json")
        pages = [
            Page(**{**_known(Page, page), "panels": [Panel(**_known(Panel, p)) for p in page.get("panels", [])]})
            for page in data.get("pages", [])
        ]
        stage = LEGACY_STAGES.get(data.get("stage"), data.get("stage"))
        return cls(
            chapter_id=data["chapter_id"],
            source_pdf=data["source_pdf"],
            work_dir=data["work_dir"],
            stage=stage if stage in STAGE_ORDER else "init",
            doc_type=data.get("doc_type", "manga"),
            pages=pages,
            beats=[Panel(**_known(Panel, b)) for b in data.get("beats", [])],
        )

    def kept_panels(self) -> List[Panel]:
        """The narration beats, in line order - what the render stages (s6 tts, s7 video) consume."""
        return list(self.beats)

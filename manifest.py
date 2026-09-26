"""The manifest is the spine of the whole pipeline.

Every stage reads it, does its work on disk, adds its fields, and advances
`stage`. Nothing is passed in memory between stages — only files + this JSON.
That is what makes each stage independently testable and swappable.

The pipeline is STORY-FIRST: stage 5 writes the whole recap script from ALL the
chapter's OCR, then stage 2b matches one character frame to each script segment.

Field ownership (which stage fills what):
    s1  -> Page.image
    s2  -> Page.panels (bbox, order, crop, speaker, dialogue)
    s2a -> Panel.has_person/has_face/person_count/face_count/is_text_only
           (+ white_fraction): LOCAL CPU vision tags so matching looks at the
           art (faces/people) instead of only OCR text. Also Panel.frame_kind
           ("closeup"/"impact"/"full") and Panel.crop_clean: close-ups are cropped
           tight (bubbles trimmed) while impact/splash panels are kept whole.
           Downstream stages SHOW the right image via Panel.frame_image.
    s5  -> STAGE A (story-first script): reads every candidate panel's OCR and
           writes the full hero-POV recap as ordered SEGMENTS to
           work/<ch>/script/segments.json (+ script.txt). Touches no panels.
    s2b -> STAGE B (frame matching): picks one eligible cropped frame per segment
           and fills `Manifest.beats` - the ordered matched narration beats (each
           a frame-panel clone carrying that segment's `script_line`). Also flags
           the matched candidates' `Panel.selected` for inspection.
    s3  -> Panel.context, Panel.emotion (on the beats)
    s4  -> Page.clean_image
    s6  -> Panel.audio, Panel.duration_s (on the beats)
    s7  -> reads the beats, renders the video

Candidate panels (Page.panels) are never reduced: STAGE A and STAGE B both read
the FULL candidate set via `ordered_panels()` on every run. The matched output
lives separately in `Manifest.beats`, which `Manifest.kept_panels()` returns so a
frame can be REUSED across segments (each beat is its own panel) without mutating
the candidate reading order.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

from atomic_io import atomic_write_json, read_json_with_backup_fallback

# Ordered progress markers. Each stage SETS the next marker when it finishes.
# Keep in sync with orchestrator.STAGES. STORY-FIRST order: the full script is
# written ("script") before frames are matched to it ("match").
STAGE_ORDER = ["init", "pages", "panels", "vision", "script", "match", "context", "clean", "audio", "video"]


@dataclass
class Panel:
    id: str
    bbox: List[float]                   # [x, y, w, h] in page-image pixels
    order: int                          # global reading order across the chapter
    crop: Optional[str] = None          # cropped panel image            (s2)
    speaker: Optional[str] = None       # speaker id / name              (s2)
    dialogue: Optional[str] = None      # raw bubble text                (s2)
    has_person: bool = False            # YOLO found >=1 person          (s2a)
    has_face: bool = False              # OpenCV found >=1 face (close-up)(s2a)
    person_count: int = 0               # number of persons detected     (s2a)
    face_count: int = 0                 # number of faces detected        (s2a)
    is_text_only: bool = False          # mostly white/text, no character (s2a)
    white_fraction: Optional[float] = None  # near-white pixel fraction   (s2a)
    crop_clean: Optional[str] = None    # crop tightened to the character, bubbles trimmed (s2a)
    frame_kind: Optional[str] = None    # "closeup" (cropped) | "impact"/"full" (kept whole) (s2a)
    selected: bool = True               # kept for the recap?           (s2b)
    context: Optional[str] = None       # scene / expression description (s3)
    emotion: Optional[str] = None       # emotion tag for TTS            (s3)
    script_line: Optional[str] = None   # narration line                (s5)
    audio: Optional[str] = None         # voiceover clip path           (s6)
    duration_s: Optional[float] = None  # clip length -> slide timing   (s6)

    @property
    def frame_image(self) -> Optional[str]:
        """The image downstream stages (s3 context, s7 video) should SHOW for
        this panel: the clean character crop (bubbles trimmed) when s2a produced
        one, else the raw candidate crop. A property, so it is never serialized."""
        return self.crop_clean or self.crop


@dataclass
class Page:
    index: int                          # 1-based page number
    image: str                          # rendered page image            (s1)
    clean_image: Optional[str] = None   # text removed                   (s4)
    panels: List[Panel] = field(default_factory=list)


@dataclass
class Manifest:
    chapter_id: str
    source_pdf: str
    work_dir: str
    stage: str = "init"
    doc_type: str = "manga"             # "manga" (page-based) or "webtoon" (strip)
    pages: List[Page] = field(default_factory=list)
    beats: List[Panel] = field(default_factory=list)  # matched narration beats (s2b)

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
            Page(
                index=p["index"],
                image=p["image"],
                clean_image=p.get("clean_image"),
                panels=[Panel(**pan) for pan in p.get("panels", [])],
            )
            for p in data.get("pages", [])
        ]
        return cls(
            chapter_id=data["chapter_id"],
            source_pdf=data["source_pdf"],
            work_dir=data["work_dir"],
            stage=data.get("stage", "init"),
            doc_type=data.get("doc_type", "manga"),
            pages=pages,
            beats=[Panel(**b) for b in data.get("beats", [])],
        )

    def all_panels(self):
        for page in self.pages:
            for panel in page.panels:
                yield page, panel

    def ordered_panels(self) -> List[Panel]:
        return sorted((pan for _, pan in self.all_panels()), key=lambda p: p.order)

    def kept_panels(self) -> List[Panel]:
        """The matched narration beats (STAGE B), in segment order - what the
        render stages (s3 context, s6 tts, s7 video) consume. Falls back to the
        legacy `selected` candidate panels for old manifests with no beats.
        `ordered_panels()` still returns the FULL candidate set so STAGE A/B always
        reconsider everything."""
        if self.beats:
            return list(self.beats)
        return [p for p in self.ordered_panels() if p.selected]

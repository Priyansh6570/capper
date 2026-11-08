"""The call-to-action clip: a short 1080p video that asks viewers to like, subscribe and comment, which a merge
can place after any chapter (services/merging.py, `cta_after`).

The clip is a normal render of stages/s9_cta.py over a chapter of the project: frames with no dialogue (found
by reading the frames of the project's chapters with the OCR and keeping the ones with no text), the chapter's
own voice and look, and a closing card. This module only prepares its plan, starts that stage and files the
result under projects/<slug>/cta/; nothing in the manifest or the pipeline stages changes.

The words are in projects/<slug>/cta/script.txt, one spoken line per row as `pace: text` (pace is optional:
calm, tense, action, reveal, sad, comic). It is created with DEFAULT_SCRIPT the first time and is yours to edit.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageStat

import config
import pacing
import projects
from config import CTA_FILE, CTA_FOLDER, CTA_PLAN, NO_CONSOLE, ROOT

DEFAULT_SCRIPT = """\
calm: Quick pause, before the next chapter.
tense: Every recap on this channel is hours of reading, scripting, voicing and editing. A small team, a lot of late nights, and real money behind it.
reveal: No studio. No sponsor. Just people who love these stories, and want you to have them every single day.
calm: If it has earned your time, a like tells us it was worth it. Subscribe, and you will never lose your place.
action: And the comments? We read every one. Tell us what you think, and which story we should cover next.
calm: Thank you for watching. Back to the story.
"""

FRAMES_PER_LINE = 2
LAST_LINE_CARD = True
SCAN_CHAPTERS = 8          # chapters whose frames are looked through
SCAN_PER_CHAPTER = 12      # frames read per chapter
MIN_SIDE = 400             # smaller frames look soft on a 1080p canvas
MAX_WHITE_LEVEL = 250         # a pixel this bright on every channel counts as white
MAX_WHITE_SHARE = 0.004       # more white than this and the frame has blanked dialogue boxes
MIN_CONTRAST = 38.0        # grey-level spread; blank or washed-out frames fall under it


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #
def cta_dir(slug: str) -> Path:
    d = projects.project_dir(slug) / CTA_FOLDER
    d.mkdir(parents=True, exist_ok=True)
    return d


def clip_path(slug: str) -> Path:
    return projects.project_dir(slug) / CTA_FOLDER / CTA_FILE


def info(slug: str) -> dict:
    """{"ready": bool, "duration_s", "built_at", "chapter", "lines"} of the project's clip."""
    clip, meta = clip_path(slug), clip_path(slug).with_name("cta.json")
    if not clip.is_file():
        return {"ready": False}
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {**data, "ready": True, "size": clip.stat().st_size}


def parse_script(text: str) -> list[dict]:
    """Rows of `pace: text` (or just text) as [{"text", "pace"}]; blank rows are skipped."""
    lines = []
    for row in text.splitlines():
        row = row.strip()
        if not row:
            continue
        head, sep, rest = row.partition(":")
        if sep and head.strip().lower() in pacing.PACES:
            lines.append({"text": rest.strip(), "pace": head.strip().lower()})
        else:
            lines.append({"text": row, "pace": "calm"})
    return [line for line in lines if line["text"]]


def own_frames(slug: str) -> list[str]:
    """Pictures made for the clip (projects/<slug>/cta/frames/, shown in name order, numbers in natural order).
    When there are some, the clip shows them, one per line, instead of frames taken from the chapters."""
    folder = cta_dir(slug) / "frames"
    files = [p for p in folder.glob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
    files.sort(key=lambda p: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.stem)])
    return [str(p) for p in files]


def load_script(slug: str) -> list[dict]:
    path = cta_dir(slug) / "script.txt"
    if not path.is_file():
        path.write_text(DEFAULT_SCRIPT, encoding="utf-8")
    return parse_script(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# frames without dialogue
# --------------------------------------------------------------------------- #
def source_chapters(slug: str) -> list[int]:
    """Chapters with frames on this PC (a cleared chapter is not restored for this), newest first."""
    found = []
    for chapter in projects.load(slug)["chapters"]:
        n = chapter["n"]
        work = projects.chapter_work_path(slug, n)
        if (work / "manifest.json").is_file() and any((work / "frames").glob("*.png")):
            found.append(n)
    return sorted(found, reverse=True)


def _contrast(path: Path) -> float:
    with Image.open(path) as image:
        return ImageStat.Stat(image.convert("L")).stddev[0]


def _white_share(path: Path) -> float:
    """Share of the frame that is pure white. The editor blanks speech bubbles with white boxes, which the OCR
    cannot read but a viewer sees as empty dialogue boxes."""
    with Image.open(path) as image:
        pixels = np.asarray(image.convert("RGB"))
    return float((pixels.min(axis=2) >= MAX_WHITE_LEVEL).mean())


def _usable(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            width, height = image.size
        return (min(width, height) >= MIN_SIDE and 0.6 <= width / height <= 2.4 and _contrast(path) >= MIN_CONTRAST
                and _white_share(path) <= MAX_WHITE_SHARE)
    except OSError:
        return False


def pick_frames(slug: str, wanted: int, chapters: list[int]) -> Iterator[str | list[str]]:
    """Yield log lines, then (last) the list of `wanted` frame paths that show no text, from chapters spread
    over `chapters`. A frame with any text the OCR can read (dialogue, caption or sound effect) is left out."""
    import ocr   # EasyOCR on torch: imported only when a clip is made

    step = max(1, len(chapters) // SCAN_CHAPTERS)
    chosen_chapters = chapters[::step][:SCAN_CHAPTERS]
    picked: list[str] = []
    for n in chosen_chapters:
        frames = sorted((projects.chapter_work_path(slug, n) / "frames").glob("*.png"))
        stride = max(1, len(frames) // SCAN_PER_CHAPTER)
        kept = 0
        for path in frames[::stride][:SCAN_PER_CHAPTER]:
            if not _usable(path):
                continue
            if ocr.read_panel(path):
                continue
            picked.append(str(path))
            kept += 1
        yield f"  chapter {n}: {kept} frame(s) without text"
    if len(picked) < wanted:
        raise RuntimeError(f"Only found {len(picked)} frame(s) without text (need {wanted}); "
                           f"the chapters have too little text-free art.")
    # spread what was found over the whole run, so the clip does not show one scene
    yield [picked[round(i * (len(picked) - 1) / max(1, wanted - 1))] for i in range(wanted)] if wanted > 1 else picked[:1]


# --------------------------------------------------------------------------- #
# the closing card
# --------------------------------------------------------------------------- #
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def end_card(dest: Path, channel: str) -> Path:
    """A 1280x720 card: LIKE / SUBSCRIBE / COMMENT with the channel name."""
    width, height = 1280, 720
    card = Image.new("RGB", (width, height), (14, 14, 20))
    draw = ImageDraw.Draw(card)
    draw.rectangle([0, 0, width, 8], fill=(230, 57, 70))
    draw.rectangle([0, height - 8, width, height], fill=(230, 57, 70))
    rows = [("LIKE", "if it was worth your time"), ("SUBSCRIBE", "so you never lose your place"),
            ("COMMENT", "what should we cover next?")]
    y = 120
    for word, sub in rows:
        draw.text((width // 2, y), word, font=_font(96), fill=(255, 255, 255), anchor="mt")
        draw.text((width // 2, y + 104), sub, font=_font(34), fill=(170, 175, 190), anchor="mt")
        y += 170
    draw.text((width // 2, height - 52), channel.upper(), font=_font(30), fill=(230, 57, 70), anchor="mm")
    dest.parent.mkdir(parents=True, exist_ok=True)
    card.save(dest)
    return dest


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def make_plan(script: list[dict], frames: list[str], card: str | None) -> dict:
    """Spread `frames` over the lines in order, FRAMES_PER_LINE each; the last line shows the card."""
    lines, k = [], 0
    for i, line in enumerate(script):
        last = i == len(script) - 1
        if last and card:
            shown = [card]
        else:
            shown = frames[k:k + FRAMES_PER_LINE]
            k += FRAMES_PER_LINE
        lines.append({**line, "frames": shown})
    return {"lines": lines}


def frames_needed(script: list[dict]) -> int:
    return FRAMES_PER_LINE * (len(script) - (1 if LAST_LINE_CARD else 0))


def build(slug: str) -> Iterator[str]:
    """Make the project's clip, yielding log lines. Raises RuntimeError when it cannot be made."""
    project = projects.load(slug)
    chapters = source_chapters(slug)
    if not chapters:
        raise RuntimeError("No chapter of this project has frames on this PC to take pictures from.")
    script = load_script(slug)
    if not script:
        raise RuntimeError("The call-to-action script is empty (projects/<slug>/cta/script.txt).")
    source = chapters[0]
    own = own_frames(slug)
    if own and len(own) < len(script):
        raise RuntimeError(f"The script has {len(script)} lines but projects/<slug>/cta/frames/ has {len(own)} picture(s).")
    yield "Using the pictures in the project's cta/frames folder." if own else f"Looking for frames without dialogue in {min(len(chapters), SCAN_CHAPTERS)} chapter(s)…"
    frames: list[str] = []
    for item in ([own[:len(script)]] if own else pick_frames(slug, frames_needed(script), chapters)):
        if isinstance(item, list):
            frames = item
        else:
            yield item
    work = projects.chapter_work_dir(slug, source)
    out_dir = projects.chapter_output_dir(slug, source)
    card = end_card(work / CTA_FOLDER / "end_card.png", project["name"]) if LAST_LINE_CARD and not own else None
    plan = ({"lines": [{**line, "frames": [frame]} for line, frame in zip(script, frames)]} if own
            else make_plan(script, frames, str(card) if card else None))
    (work / CTA_FOLDER).mkdir(exist_ok=True)
    (work / CTA_FOLDER / CTA_PLAN).write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    yield f"{len(script)} lines, {len(frames)} frames; voicing and rendering with chapter {source}'s look…"

    chapter_id = json.loads((work / "manifest.json").read_text(encoding="utf-8"))["chapter_id"]
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONFAULTHANDLER="1",
               MANHWA_WORK_ROOT=str(work), MANHWA_OUTPUT_ROOT=str(out_dir))
    with config.chapter_scope(work, out_dir):
        projects.write_media_snapshot(slug, source)
    proc = subprocess.Popen([sys.executable, "-u", "-m", "stages.s9_cta", "--chapter", chapter_id], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                            bufsize=1, encoding="utf-8", errors="replace", **NO_CONSOLE)
    try:
        for row in proc.stdout:
            yield row.rstrip("\n")
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
    made = out_dir / CTA_FOLDER / CTA_FILE
    if proc.returncode or not made.is_file():
        raise RuntimeError(f"The render exited {proc.returncode}; see the log above.")

    dest = clip_path(slug)
    shutil.copy2(made, dest)
    stage_meta = {}
    try:
        stage_meta = json.loads((made.with_name("cta.json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    dest.with_name("cta.json").write_text(json.dumps({
        **stage_meta, "chapter": source, "lines": len(script), "frames": len(frames),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}), encoding="utf-8")
    yield f"Done: {dest}"

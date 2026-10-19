"""Shorts: picking the strongest segments of a finished chapter with the local `claude` CLI, and the
files of a chapter's shorts. The segments are rendered by stages/s8_shorts.py from the plan saved here.

A segment is a run of the chapter's narration lines. The model reads the script (every line with its
pace and playing time) and returns only the segments that would hold a viewer; how many that is, or
none, is its call. The plan is checked locally against the real render length before it is saved.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable

import claude_cli
from atomic_io import atomic_write_json, read_json_with_backup_fallback
from config import SHORTS_FOLDER, SHORTS_INDEX, SHORTS_PLAN
from manifest import Manifest, Panel
from services.common import now_iso

MAX_SHORT_S = 59.0    # a short plays under a minute
MIN_SHORT_S = 10.0    # anything shorter has no room for a story

SCHEMA = {
    "type": "object",
    "required": ["shorts"],
    "properties": {"shorts": {"type": "array", "items": {
        "type": "object", "required": ["first_line", "last_line", "title", "description", "reason"],
        "properties": {"first_line": {"type": "integer"}, "last_line": {"type": "integer"},
                       "title": {"type": "string"}, "description": {"type": "string"},
                       "reason": {"type": "string"}}}}},
}


def plan_path(work: Path) -> Path:
    return work / SHORTS_FOLDER / SHORTS_PLAN


def shorts_dir(output: Path) -> Path:
    return output / SHORTS_FOLDER


def script_beats(work: Path) -> list[Panel]:
    """The chapter's narration lines with their voiced audio, in order. Raises ValueError when the chapter
    has no script or lost its audio, since a short reuses both."""
    beats = [b for b in Manifest.load(work).kept_panels() if (b.script_line or "").strip()]
    if not beats:
        raise ValueError("This chapter has no script.")
    if unvoiced := [b.id for b in beats if not (b.audio and Path(b.audio).is_file())]:
        raise ValueError(f"The narration audio of {', '.join(unvoiced[:5])} is missing; render the chapter's audio first.")
    return beats


def script_lines(beats: list[Panel]) -> str:
    return "\n".join(f"{b.order} | {b.emotion or 'calm'} | {b.duration_s or 0:.0f}s | {b.script_line.strip()}"
                     for b in beats)


def _prompt(chapter_no: int, beats: list[Panel], brief: str) -> str:
    return (
        f"You are picking clips for YouTube Shorts and TikTok from chapter {chapter_no} of a manhwa recap video. "
        "Below is the chapter's narration, one numbered line per row: `line | pace | seconds it plays | text`. "
        "Each line is voiced over its own panels, so a clip is a run of consecutive lines.\n\n"
        f"Series brief:\n{brief.strip() or '(none)'}\n\nNarration:\n{script_lines(beats)}\n\n"
        "Pick the segments that would make a strong short: interesting or mysterious moments that build "
        "curiosity, violent, epic or fighting scenes, anger, or any other strong emotion.\n"
        "- Choose only what is strong enough to stand on its own. The number of shorts is decided by relevance "
        "alone: one, five or none at all (return an empty list) can each be right. Never add a weaker segment "
        "to reach a count.\n"
        f"- A segment is `first_line` to `last_line`, both included. The seconds of its lines added together "
        f"must stay under {MAX_SHORT_S - 4:.0f}.\n"
        "- It has to work without the rest of the chapter: start on a line that grabs attention and end on a "
        "payoff, a turn or a cliffhanger. Segments must not overlap.\n"
        "- `title`: specific to the segment and truthful, under 70 characters, written to make people watch. "
        "`description`: one to three sentences for the viewer that say what the segment is about.\n"
        "- `reason`: one sentence on why this segment is worth a short.")


def _fits(start: int, end: int, beats: list[Panel]) -> list[Panel]:
    return [b for b in beats if start <= b.order <= end]


def accept(proposed: list[dict], beats: list[Panel],
           seconds_of: Callable[[list[Panel]], float]) -> tuple[list[dict], list[str]]:
    """The proposed segments that can be made, as plan entries, and a note for each one that was dropped.
    `seconds_of` is the real render length of a run of beats. A segment over the limit loses its last
    lines until it fits; overlaps with an earlier segment, empty ones and too short ones are dropped."""
    accepted, notes, taken = [], [], set()
    for item in sorted(proposed, key=lambda s: s["first_line"]):
        title, description = item["title"].strip(), item["description"].strip()
        span = _fits(item["first_line"], item["last_line"], beats)
        if not (span and title and description):
            notes.append(f"Dropped lines {item['first_line']}-{item['last_line']}: no lines or no title/description")
            continue
        if taken & {b.order for b in span}:
            notes.append(f"Dropped lines {item['first_line']}-{item['last_line']}: overlaps an earlier short")
            continue
        while len(span) > 1 and seconds_of(span) >= MAX_SHORT_S:
            span.pop()
        seconds = seconds_of(span)
        if not MIN_SHORT_S <= seconds < MAX_SHORT_S:
            notes.append(f"Dropped lines {item['first_line']}-{item['last_line']}: {seconds:.0f}s is out of range")
            continue
        taken.update(b.order for b in span)
        accepted.append({"id": len(accepted) + 1, "first_line": span[0].order, "last_line": span[-1].order,
                         "duration_s": round(seconds, 1), "title": title, "description": description,
                         "reason": item["reason"].strip()})
    return accepted, notes


def find(*, chapter_no: int, beats: list[Panel], brief: str, model: str, work: Path,
         seconds_of: Callable[[list[Panel]], float], on_event: Callable[[dict], None],
         on_limit: Callable[[datetime], None], on_proc: Callable[[subprocess.Popen], None],
         before_call: Callable[[], None] | None) -> dict:
    """Ask the model for the chapter's segments. Returns {shorts, notes, usage}."""
    usage = claude_cli.empty_usage()
    work.mkdir(parents=True, exist_ok=True)
    answer = claude_cli.call([{"type": "text", "text": _prompt(chapter_no, beats, brief)}], SCHEMA, model, work,
                             usage=usage, mention_event="read", panel_ids=set(), on_proc=on_proc,
                             on_event=on_event, on_limit=on_limit, before_call=before_call)
    shorts, notes = accept(answer["shorts"], beats, seconds_of)
    return {"shorts": shorts, "notes": notes, "usage": usage}


def clear(work: Path, output: Path) -> None:
    """Delete the chapter's shorts: the plan, the videos and their index."""
    shutil.rmtree(work / SHORTS_FOLDER, ignore_errors=True)
    shutil.rmtree(shorts_dir(output), ignore_errors=True)


def save_plan(work: Path, output: Path, chapter_id: str, shorts: list[dict]) -> None:
    """Replace whatever shorts the chapter had with this plan (empty: the chapter has none)."""
    clear(work, output)
    path = plan_path(work)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"chapter_id": chapter_id, "saved_at": now_iso(), "shorts": shorts})


def read_plan(work: Path) -> list[dict]:
    try:
        return read_json_with_backup_fallback(plan_path(work))["shorts"]
    except (OSError, ValueError, KeyError):
        return []


def write_zip(output: Path, dest: Path) -> None:
    """The chapter's rendered shorts in one zip, with a titles.txt of their titles and descriptions."""
    made = rendered(output)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as archive:
        for short in made:
            archive.write(shorts_dir(output) / short["file"], short["file"])
        archive.writestr("titles.txt", "\n\n".join(f"{s['file']}\n{s['title']}\n{s['description']}" for s in made))


def rendered(output: Path) -> list[dict]:
    """The chapter's rendered shorts, each with its title, description and file."""
    try:
        shorts = json.loads((shorts_dir(output) / SHORTS_INDEX).read_text(encoding="utf-8"))["shorts"]
    except (OSError, ValueError, KeyError):
        return []
    return [s for s in shorts if (shorts_dir(output) / s["file"]).is_file()]

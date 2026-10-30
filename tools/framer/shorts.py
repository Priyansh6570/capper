"""Shorts: picking the strongest moments of a run of finished chapters with the local `claude` CLI, and the
files of a chapter's shorts. The segments are rendered by stages/s8_shorts.py from the plan saved here.

A segment is a run of one chapter's narration lines. The model reads the scripts of the whole range (every
line with its pace and playing time), rates the moments that would catch a viewer (fast action, comedy, drama,
mystery) against each other and returns the strong ones; when the creator asked for a number it fills that
number by score from across the range, skipping chapters with nothing strong. Each chapter's plan is checked locally against the real render length before it is saved.

A short is not the scene played as it is: a second call per picked moment (`write_script`) writes a new, tight
recap narration of the scene that ends on a teaser made for that scene, and says which of the scene's panels
to show with each line. stages/s8_shorts.py voices that script and renders it.
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
MAX_SOURCE_S = 150.0  # the scene a short retells, as it plays in the chapter video; the short condenses it
MIN_SHORT_S = 10.0    # anything shorter has no room for a story

MIN_SCORE = 6         # a moment the model rates under this is not worth a short
SPARES = 2            # candidates asked for beyond the wanted number, for ones the local checks drop
GROUP_CHARS = 150_000  # the script text sent in one call; a longer range is read in several calls

SCHEMA = {
    "type": "object",
    "required": ["shorts"],
    "properties": {"shorts": {"type": "array", "items": {
        "type": "object",
        "required": ["chapter", "first_line", "last_line", "title", "description", "reason", "score"],
        "properties": {"chapter": {"type": "integer"}, "first_line": {"type": "integer"},
                       "last_line": {"type": "integer"}, "title": {"type": "string"},
                       "description": {"type": "string"}, "reason": {"type": "string"},
                       "score": {"type": "integer"}}}}},
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


def chapter_groups(scripts: dict[int, list[Panel]]) -> list[list[int]]:
    """The chapters (in order) split into runs whose script text fits one call."""
    groups, size = [[]], 0
    for n in sorted(scripts):
        chars = len(script_lines(scripts[n]))
        if groups[-1] and size + chars > GROUP_CHARS:
            groups.append([])
            size = 0
        groups[-1].append(n)
        size += chars
    return groups


def _prompt(scripts: dict[int, list[Panel]], numbers: list[int], brief: str, part: tuple[int, int],
            max_shorts: int | None = None) -> str:
    chapters = "\n\n".join(f"=== CHAPTER {n} ===\n{script_lines(scripts[n])}" for n in numbers)
    reading = (f"This is part {part[0]} of {part[1]} of the range; rate every moment on the same absolute scale.\n"
               if part[1] > 1 else "")
    return (
        "You are picking clips for YouTube Shorts and TikTok from a run of chapters of a manhwa recap video. "
        "Below is each chapter's narration, one numbered line per row: `line | pace | seconds it plays | text`. "
        "Each line is voiced over its own panels, so a clip is a run of consecutive lines of one chapter.\n"
        f"{reading}\nSeries brief:\n{brief.strip() or '(none)'}\n\nNarration:\n{chapters}\n\n"
        "Study the whole run and find the moments viewers of short videos actually stop for. Favour, in this "
        "order: (1) fast-paced ACTION (a fight, a chase, a power-up, a sudden attack, a dramatic reversal), "
        "(2) COMEDY (a funny reaction, a gag, an absurd or embarrassing situation, a punchline), "
        "(3) DRAMA (a confrontation, betrayal, humiliation, a revenge or triumph, a heartbreak) and "
        "(4) MYSTERY (a secret, a twist, a cliffhanger the viewer has to have answered). Lines paced `action`, "
        "`tense`, `reveal` or `comic` are the likely places; `calm` stretches (set-up, explanation, travel, "
        "recaps of earlier events) make bad shorts, so skip them unless a line in them lands a punch.\n"
        "- Choose for energy: a clip should keep something happening every few seconds, with no slow "
        "stretches inside it, hooked from its first line to a clear payoff.\n"
        f"{_count_rule(max_shorts)}"
        f"- `score`: 1 to 10, how well the clip would hold a stranger scrolling past. Return only segments of "
        f"{MIN_SCORE} or more: {MIN_SCORE}-7 is decent, 8 is good, 9-10 would stop the scroll on its own. Judge "
        "the chapters against each other on the same scale.\n"
        f"- A segment is `chapter`, `first_line` to `last_line`, both included. The seconds of its lines added "
        f"together must stay under {MAX_SOURCE_S:.0f}. Take the WHOLE scene, from where it starts to where "
        f"it pays off ({MIN_SHORT_S:.0f}s at the very least): the short will not play these lines but retell "
        "the scene in under a minute, so the segment must be one self-contained, gripping scene.\n"
        "- It has to work without the rest of the story: start on a line that grabs attention and end on a "
        "payoff, a turn or a cliffhanger. Segments must not overlap.\n"
        "- `title`: specific to the segment and truthful, under 70 characters, written to make people watch. "
        "`description`: one to three sentences for the viewer that say what the segment is about.\n"
        "- `reason`: one sentence on why this segment is worth a short.")


def _count_rule(max_shorts: int | None) -> str:
    if max_shorts is None:
        return ("- Be selective: return only the clearly strong moments, and not several from one scene. Fewer "
                "is fine, even none.\n")
    return (f"- The creator wants at most {max_shorts} short{'s' if max_shorts != 1 else ''} from this run. Return "
            f"the {max_shorts + SPARES} best moments of the WHOLE run, wherever they are: judge every chapter "
            "first, then keep the highest scores. Do not favour early chapters or spread picks evenly; skip any "
            "chapter with nothing strong, and take several from one chapter if they are that good (never two "
            f"from the same scene). Fewer is right when fewer score {MIN_SCORE} or more; return none if nothing does.\n")


def _fits(start: int, end: int, beats: list[Panel]) -> list[Panel]:
    return [b for b in beats if start <= b.order <= end]


def accept(proposed: list[dict], beats: list[Panel],
           seconds_of: Callable[[list[Panel]], float], max_s: float = MAX_SHORT_S) -> tuple[list[dict], list[str]]:
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
        while len(span) > 1 and seconds_of(span) >= max_s:
            span.pop()
        seconds = seconds_of(span)
        if not MIN_SHORT_S <= seconds < max_s:
            notes.append(f"Dropped lines {item['first_line']}-{item['last_line']}: {seconds:.0f}s is out of range")
            continue
        taken.update(b.order for b in span)
        accepted.append({"id": len(accepted) + 1, "first_line": span[0].order, "last_line": span[-1].order,
                         "duration_s": round(seconds, 1), "title": title, "description": description,
                         "reason": item["reason"].strip(),
                         **({"score": item["score"]} if "score" in item else {})})
    return accepted, notes


def select(by_chapter: dict[int, list[dict]], max_shorts: int | None) -> dict[int, list[dict]]:
    """The accepted segments of every chapter, cut to `max_shorts` (None: all of them), ranked over the
    chapters by merit alone: the best scores of the whole range win, whichever chapters they are in (a chapter
    may get several, or none). Only among equal scores does a chapter's best segment go before its second.
    A chapter left with none is not listed; the ones kept are numbered again in story order."""
    ranked = []
    for n, shorts in by_chapter.items():
        for rank, short in enumerate(sorted(shorts, key=lambda x: (-x.get("score", 0), x["id"]))):
            ranked.append((-short.get("score", 0), rank, n, short["id"]))
    keep = {(n, number) for _, _, n, number in sorted(ranked)[:max_shorts]}
    chosen = {}
    for n, shorts in sorted(by_chapter.items()):
        kept = [short for short in shorts if max_shorts is None or (n, short["id"]) in keep]
        if kept:
            chosen[n] = [{**short, "id": i} for i, short in enumerate(kept, start=1)]
    return chosen


def find_range(*, scripts: dict[int, list[Panel]], brief: str, model: str, work: Path, max_shorts: int | None,
               seconds_of: Callable[[int, list[Panel]], float], on_event: Callable[[dict], None],
               on_limit: Callable[[datetime], None], on_proc: Callable[[subprocess.Popen], None],
               before_call: Callable[[], None] | None, on_progress: Callable[[float], None] = lambda fraction: None
               ) -> dict:
    """Ask the model for the strongest moments of a run of chapters (`scripts`: chapter -> its voiced lines).
    Returns {shorts: {chapter: plan entries}, notes, usage}; chapters without a strong moment are not in it."""
    usage = claude_cli.empty_usage()
    work.mkdir(parents=True, exist_ok=True)
    groups = chapter_groups(scripts)
    proposed: dict[int, list[dict]] = {}
    for part, numbers in enumerate(groups, start=1):
        on_progress((part - 1) / len(groups))
        answer = claude_cli.call([{"type": "text", "text": _prompt(scripts, numbers, brief, (part, len(groups)), max_shorts)}],
                                 SCHEMA, model, work, usage=usage, mention_event="read", panel_ids=set(),
                                 on_proc=on_proc, on_event=on_event, on_limit=on_limit, before_call=before_call)
        for item in answer["shorts"]:
            if item["chapter"] in numbers and item["score"] >= MIN_SCORE:
                proposed.setdefault(item["chapter"], []).append(item)
    accepted, notes = {}, []
    for n, items in sorted(proposed.items()):
        entries, dropped = accept(items, scripts[n], lambda run, n=n: seconds_of(n, run), MAX_SOURCE_S)
        notes += [f"Chapter {n}: {note}" for note in dropped]
        if entries:
            accepted[n] = entries
    return {"shorts": select(accepted, max_shorts), "notes": notes, "usage": usage}


WORDS_PER_S = 2.5      # how fast the narration voice speaks, pauses included
BODY_WORDS = 85        # the recap of the scene
CLOSING_WORDS = 30     # the teaser that ends the short
MAX_WORDS = 135        # a script over this is trimmed: it would not fit under a minute

_LINE = {"type": "object", "required": ["text", "pace", "source_lines"],
         "properties": {"text": {"type": "string"}, "pace": {"type": "string"},
                        "source_lines": {"type": "array", "items": {"type": "integer"}}}}
SCRIPT_SCHEMA = {"type": "object", "required": ["lines", "closing"],
                 "properties": {"lines": {"type": "array", "items": _LINE}, "closing": _LINE,
                                "track": {"type": "string"}, "peak_line": {"type": "integer"}}}


def _track_rule(tracks: list[dict] | None) -> str:
    if not tracks:
        return ""
    menu = "\n".join(f"  {t['id']} | {t['theme']} | {t['duration_s']:.0f}s | "
                     f"{('drop at %.0fs' % t['drop_s']) if t.get('drop_s') else 'no drop'}" for t in tracks)
    return (
        "\n- `track`: the id of the background music for this short, chosen for the scene's mood at its peak "
        f"and for a length the story fits (`id | theme | seconds | drop`):\n{menu}\n"
        "  The short runs exactly as long as its track, so write the narration to fit it: about "
        f"{WORDS_PER_S:g} words per second of the track in all (lines and closing together), done about a second before "
        "the track ends; this replaces the word count above.\n"
        "- `peak_line`: when the chosen track has a drop, the 0-based index in `lines` of the scene's single biggest "
        "moment (the strongest panel, the blow, the reveal). That line starts exactly at the drop, so the lines before "
        "it must take about as many seconds to say as the drop's time (the build-up), and the lines from it on plus "
        "the closing the rest of the track. Without a drop, or without a track, use -1.")


def _script_prompt(short: dict, scene: list[Panel], after: list[Panel], brief: str, tracks: list[dict] | None = None) -> str:
    return (
        "You are writing the narration of a YouTube Short / TikTok that retells ONE scene of a manhwa, from the "
        "narration of a longer recap video. The short is voiced and shown over the scene's panels.\n\n"
        f"Series brief:\n{brief.strip() or '(none)'}\n\n"
        f"The scene: \"{short['title']}\" - {short['description']}\n\n"
        f"Its narration in the recap video, `line | pace | seconds | text`:\n{script_lines(scene)}\n\n"
        "What happens right after it (never reveal this):\n"
        f"{script_lines(after) or '(the chapter ends)'}\n\n"
        "Write the short's narration:\n"
        f"- `lines`: a tight recap of the scene, about {BODY_WORDS} words in all, in the same narrator voice "
        "(first or third person) as the lines above. Open on a hook that makes a stranger stay, then explain what "
        "is going on and who these people are only as far as the scene needs, then drive to the peak. Short, "
        "punchy sentences; fast; no filler, no 'in this scene'. One to two sentences per line.\n"
        "- `pace` of each line: one of calm, tense, action, reveal, sad, comic.\n"
        "- `source_lines`: for each line, one to three of the scene's line numbers whose panels show what that "
        "line says best (a striking action pose, a face, a reveal). Pick the best-looking panels, in story order; "
        "do not repeat the same line number across lines unless nothing else fits.\n"
        f"- `closing`: about {CLOSING_WORDS} words that end the short on a mysterious teaser made for THIS scene "
        "alone: stop on the question it leaves open (never answer it), and tell the viewer that to find out what "
        "happens next they should watch this manhwa on our channel or subscribe, and that the playlist link is in "
        "the description and the pinned comment. Word it freshly for this scene, in the narrator's voice; it "
        "must not read like a generic ad. Its `source_lines` are the scene's most intriguing panels."
        f"{_track_rule(tracks)}")


def _script_entries(items: list[dict], orders: list[int], closing: bool = False) -> list[dict]:
    """Script lines with the source line numbers cleaned: only ones of the scene, at least one per line."""
    entries = []
    for i, item in enumerate(items):
        text = (item.get("text") or "").strip()
        if not text:
            continue
        sources = [n for n in dict.fromkeys(item.get("source_lines") or []) if n in orders][:3]
        if not sources:   # spread the line over the scene by its place in the script
            sources = [orders[round(i / max(1, len(items) - 1) * (len(orders) - 1))]]
        entries.append({"text": text, "pace": item.get("pace") or "calm", "source_lines": sources,
                        **({"closing": True} if closing else {})})
    return entries


def write_script(short: dict, scene: list[Panel], after: list[Panel], *, brief: str, model: str, work: Path,
                 usage: dict, tracks: list[dict] | None = None, **callbacks) -> list[dict]:
    """The recap narration for one picked moment: lines, then the closing teaser (flagged `closing`), each with
    the scene's line numbers whose panels to show. Raises ValueError when the answer has no usable script.
    With `tracks` (the project's music library) the model also picks the track to play, written to fit its length
    and its drop: stored as `short["track"]` (its id), `short["theme"]` and, for a track with a drop, `short["peak"]`
    (the 0-based index of the script line that starts at the drop)."""
    answer = claude_cli.call([{"type": "text", "text": _script_prompt(short, scene, after, brief, tracks)}], SCRIPT_SCHEMA,
                             model, work, usage=usage, mention_event="read", panel_ids=set(), **callbacks)
    orders = [b.order for b in scene]
    body = _script_entries(answer.get("lines") or [], orders)
    end = _script_entries([answer.get("closing") or {}], orders, closing=True)
    if not body or not end:
        raise ValueError("the answer had no script or no closing")
    track = next((t for t in tracks or [] if t["id"] == str(answer.get("track") or "").strip()), None)
    max_words = round(track["duration_s"] * WORDS_PER_S) if track else MAX_WORDS
    while len(body) > 1 and sum(len(e["text"].split()) for e in body + end) > max_words:
        body.pop()   # too long for a minute: the recap loses its last lines, the teaser stays
    if track:
        short.update(track=track["id"], theme=track["theme"])
        peak = answer.get("peak_line")
        if track.get("drop_s") and isinstance(peak, int) and 0 <= peak < len(body):
            short["peak"] = peak
    return body + end


def script_seconds(script: list[dict]) -> float:
    return round(sum(len(e["text"].split()) for e in script) / WORDS_PER_S, 1)


def clear(work: Path, output: Path) -> None:
    """Delete the chapter's shorts: the plan, the videos and their index."""
    shutil.rmtree(work / SHORTS_FOLDER, ignore_errors=True)
    shutil.rmtree(shorts_dir(output), ignore_errors=True)


def delete_short(work: Path, output: Path, short_id: int) -> bool:
    """Delete one short of the chapter: its video, its entry in the index and in the plan. The last one going
    takes the folders with it. False when the chapter has no such short."""
    plan = read_plan(work)
    made = rendered(output)
    if short_id not in {s["id"] for s in plan} | {s["id"] for s in made}:
        return False
    for short in made:
        if short["id"] == short_id:
            (shorts_dir(output) / short["file"]).unlink(missing_ok=True)
    left_plan = [s for s in plan if s["id"] != short_id]
    left_made = [s for s in made if s["id"] != short_id]
    if not left_plan and not left_made:
        clear(work, output)
        return True
    if plan:
        atomic_write_json(plan_path(work), {**read_json_with_backup_fallback(plan_path(work)), "shorts": left_plan})
    if made:
        atomic_write_json(shorts_dir(output) / SHORTS_INDEX, {**json.loads((shorts_dir(output) / SHORTS_INDEX)
                                                                      .read_text(encoding="utf-8")), "shorts": left_made})
    return True


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

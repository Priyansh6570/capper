"""Script engine that turns a chapter's panels into narration blocks with the local
`claude` CLI instead of the Gemini API.

Pass 1 sends the panel images (downscaled copies, in batches carrying the previous
breakdown forward) and returns a factual breakdown plus a per-panel catalogue. Pass 2
is a fresh, text-only call that writes the narration from that breakdown and picks
frames from the catalogue. Frame limits and bubble blur boxes are then applied
locally. Progress and token totals are saved after each batch and pass; the
breakdown also feeds the project's story-so-far summary and character sheet.
"""
from __future__ import annotations

import base64
import io
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from PIL import Image

import bubbles
import claude_cli
import panel_split
from atomic_io import atomic_write_json, read_json_with_backup_fallback
from manifest import Panel

SEND_MAX_SIDE = 1568        # longest side of the copy of a panel sent to the model, px
JPEG_QUALITY = 85
BATCH_PANELS = 40           # images per pass-1 call (the API allows 100 and caps request size)
FRAME_KINDS = ("character", "action", "scene", "sound_effect")
KINDS = FRAME_KINDS + ("dialogue_bubble", "junk")
MAX_FRAMES = 3
DEFAULT_CHARS_PER_SEC = 15  # speaking rate assumed until a project has generated audio to measure
CHARS_PER_WORD = 6
WORDS_PER_BLOCK = 35
LENGTH_TOLERANCE = 0.9      # narration at least this share of the target length counts as reaching it

_STRINGS = {"type": "array", "items": {"type": "string"}}


def _named(key: str) -> dict:
    return {"type": "array", "items": {
        "type": "object", "required": ["name", key],
        "properties": {"name": {"type": "string"}, key: {"type": "string"}}}}


BREAKDOWN_SCHEMA = {
    "type": "object",
    "required": ["catalogue", "scenes", "characters", "uncertain", "chapter_summary"],
    "properties": {
        "catalogue": {"type": "array", "items": {
            "type": "object", "required": ["id", "description", "kind", "has_bubble"],
            "properties": {"id": {"type": "string"}, "description": {"type": "string"},
                           "kind": {"type": "string", "enum": list(KINDS)},
                           "has_bubble": {"type": "boolean"}}}},
        "scenes": {"type": "array", "items": {
            "type": "object",
            "required": ["panel_ids", "location", "characters_present", "what_happens", "wants",
                         "objects", "importance", "feelings", "stakes", "world_info"],
            "properties": {
                "panel_ids": _STRINGS,
                "location": {"type": "string"},
                "characters_present": _STRINGS,
                "what_happens": {"type": "string"},
                "wants": _named("wants"),
                "objects": {"type": "array", "items": {
                    "type": "object", "required": ["name_in_dialogue", "role"],
                    "properties": {"name_in_dialogue": {"type": "string"}, "role": {"type": "string"}}}},
                "importance": {"type": "string", "enum": ["normal", "turning_point", "reveal", "emotional_peak"]},
                "feelings": {"type": "string"},
                "stakes": {"type": "string"},
                "world_info": {"type": "string"},
            }}},
        "characters": _named("description"),
        "uncertain": {"type": "array", "items": {
            "type": "object", "required": ["panel_ids", "note"],
            "properties": {"panel_ids": _STRINGS, "note": {"type": "string"}}}},
        "chapter_summary": {"type": "string"},
    },
}

NARRATION_SCHEMA = {
    "type": "object",
    "required": ["blocks"],
    "properties": {"blocks": {"type": "array", "items": {
        "type": "object", "required": ["narration", "panel_ids"],
        "properties": {"narration": {"type": "string"}, "panel_ids": _STRINGS}}}},
}


def load_memory(path: Path) -> dict:
    try:
        memory = read_json_with_backup_fallback(path)
    except (OSError, ValueError):
        memory = {}
    return {"chapters": memory.get("chapters", {}), "characters": memory.get("characters", {})}


def _story_so_far(memory: dict, ch_no: int) -> str:
    earlier = sorted((int(n), text) for n, text in memory["chapters"].items() if int(n) < ch_no)
    return "\n".join(f"Chapter {n}: {text}" for n, text in earlier) or "(this is the first chapter)"


def _character_sheet(memory: dict) -> str:
    return "\n".join(f"- {name}: {text}" for name, text in memory["characters"].items()) or "(none yet)"


def _context(memory: dict, ch_no: int, brief: str) -> str:
    return (f"Series brief:\n{brief.strip() or '(none)'}\n\n"
            f"Story so far:\n{_story_so_far(memory, ch_no)}\n\n"
            f"Character sheet:\n{_character_sheet(memory)}")


def _batches(ids: list[str]) -> list[list[str]]:
    count = math.ceil(len(ids) / BATCH_PANELS)
    size = math.ceil(len(ids) / count)
    return [ids[i:i + size] for i in range(0, len(ids), size)]


def _image_block(panel: Panel) -> dict:
    with Image.open(panel.crop) as image:
        image = image.convert("RGB")
        image.thumbnail((SEND_MAX_SIDE, SEND_MAX_SIDE))
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=JPEG_QUALITY)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(buffer.getvalue()).decode()}}


def _batch_content(panels: list[Panel], context: str, earlier: list[dict], ch_no: int,
                   number: int, count: int) -> list[dict]:
    text = (f"You are preparing a recap of chapter {ch_no} of a manhwa. Below are panels "
            f"{panels[0].id} to {panels[-1].id} (batch {number} of {count}), each preceded by its id, in "
            "reading order.\n\n" + context + "\n\n")
    if earlier:
        carried = {k: v for k, v in _merge(earlier).items() if k != "catalogue"}
        text += f"Earlier in this chapter (from the previous batches):\n{json.dumps(carried, ensure_ascii=False)}\n\n"
    text += (
        "Return a factual breakdown. `catalogue`: one entry for EVERY panel in this batch with its id, a "
        "one-line description, its kind (character, action, scene, sound_effect; dialogue_bubble when the "
        "panel is mainly a speech bubble or text; junk for title cards, logos, credits, ads or anything "
        "that is not story art) and whether it contains a dialogue bubble. `scenes`: scene by scene with "
        "the panel ids it spans, the location, who is present, what happens (every distinct beat in "
        "order, with the important dialogue, in enough detail to narrate from), what each character "
        "wants, any plot-relevant object (named exactly as the dialogue names it, or an empty name if "
        "dialogue never does), the scene's importance as the story's mood shows it (turning_point, "
        "reveal, emotional_peak, else normal), what the character visibly feels and thinks, what is at "
        "stake for them, and any rule, power system, background or world detail the scene explains or "
        "shows (each empty when the chapter shows nothing). `characters`: everyone who appears, updating their sheet entry. "
        "`chapter_summary`: the whole chapter so far. Never guess. Anything you cannot establish from the "
        "panels and the context, such as a name, a relationship or what an object is, goes into "
        "`uncertain` with its panel ids.")
    content = [{"type": "text", "text": text}]
    for panel in panels:
        content += [{"type": "text", "text": f"{panel.id}:"}, _image_block(panel)]
    return content


def _merge(results: list[dict]) -> dict:
    characters: dict[str, str] = {}
    for result in results:
        characters.update({c["name"]: c["description"] for c in result["characters"]})
    return {"catalogue": [entry for r in results for entry in r["catalogue"]],
            "scenes": [scene for r in results for scene in r["scenes"]],
            "characters": [{"name": n, "description": d} for n, d in characters.items()],
            "uncertain": [note for r in results for note in r["uncertain"]],
            "chapter_summary": results[-1]["chapter_summary"]}


def _wants_text(scene: dict) -> str:
    return "; ".join(f"{w['name']}: {w['wants']}" for w in scene["wants"])


def _has_art(scene: dict, kinds: dict[str, str]) -> bool:
    return any(kinds.get(i) in FRAME_KINDS for i in scene["panel_ids"])


def _kinds(breakdown: dict) -> dict[str, str]:
    return {e["id"]: e["kind"] for e in breakdown["catalogue"]}


def _narration_prompt(memory: dict, ch_no: int, brief: str, breakdown: dict, style: str,
                      target_minutes: float, chars_per_sec: float) -> str:
    kinds = _kinds(breakdown)
    frames = [e for e in breakdown["catalogue"] if e["kind"] in FRAME_KINDS]
    scenes = "\n".join(
        f"[{s['panel_ids'][0]}-{s['panel_ids'][-1]}] {s['importance']} | {s['location']} | "
        f"{', '.join(s['characters_present'])} | {s['what_happens']} | wants: {_wants_text(s)} | "
        f"feelings: {s['feelings']} | stakes: {s['stakes']} | world: {s['world_info']}"
        for s in breakdown["scenes"] if _has_art(s, kinds))
    catalogue = "\n".join(f"{e['id']} | {e['kind']} | {'bubble' if e['has_bubble'] else 'no bubble'} | "
                          f"{e['description']}" for e in frames)
    uncertain = "\n".join(f"{', '.join(u['panel_ids'])}: {u['note']}" for u in breakdown["uncertain"]) or "(none)"
    words = round(target_minutes * 60 * chars_per_sec / CHARS_PER_WORD)
    return (
        f"You are writing the recap narration for chapter {ch_no} of a manhwa, working only from the notes "
        f"below (you have no images).\n\n{_context(memory, ch_no, brief)}\n\n"
        f"Chapter summary:\n{breakdown['chapter_summary']}\n\nScenes, in order:\n{scenes}\n\n"
        f"Uncertain (do not resolve these):\n{uncertain}\n\n"
        f"Frame candidates (the only panels you may use):\n{catalogue}\n\n{style}\n\n"
        "Return ordered blocks.\n"
        "- Start with the chapter's first scene and narrate every scene in order; none may be skipped. "
        "Never narrate title cards, logos, credits, ads or chapter-end cards, and never mention that the "
        "chapter or an episode ended.\n"
        "- Pace: one story beat per block, one to three sentences, slow and clear, in story order. The "
        f"narration must total {words} words or a little more (about {target_minutes:g} minutes read aloud), "
        f"which is roughly {words // WORDS_PER_BLOCK} blocks of about {WORDS_PER_BLOCK} words.\n"
        "- Reach that length through depth, never padding: give every beat in a scene its own block; at "
        "turning points, reveals and emotional peaks explain what the character feels and is thinking right "
        "then; say what is at stake; explain the world or power system when a scene shows it; and link to "
        "earlier events from the story so far when they bear on what happens. Ground all of it in the "
        "notes. Never invent plot, names, motives or facts they do not support, never repeat what a "
        "previous block already said, and add no filler. If the notes cannot support the full length, stop "
        "where they run out.\n"
        "- Narrate events and their meaning, never what is visible in a panel. If an object's identity is "
        "unclear, call it what the dialogue calls it, or leave it unnamed.\n"
        "- `panel_ids`: one to three frames that best show the moment, strongest first, only from the "
        "candidates. Prefer frames without a bubble; a frame with a bubble is fine when it is clearly the "
        "best one.")


def _extension_prompt(prompt: str, blocks: list[dict], target_minutes: float) -> str:
    draft = "\n".join(f"{n}. {b['narration']}" for n, b in enumerate(blocks, start=1))
    return (f"{prompt}\n\nYour first draft runs {_seconds(blocks) / 60:.1f} minutes against a target of "
            f"{target_minutes:g}:\n{draft}\n\nWrite the complete narration again, longer. Keep every beat "
            "that works and add depth where the notes support it: more beats within scenes, what the "
            "character feels and thinks, what is at stake, how the world or power system works, links to "
            "earlier events. Do not repeat anything already said and add no filler; if the notes run out, "
            "stop there.")


def _skipped_scenes(breakdown: dict, blocks: list[dict]) -> list[dict]:
    """Scenes with usable art that no block's frames fall in."""
    kinds = _kinds(breakdown)
    used = {i for block in blocks for i in block["panel_ids"]}
    return [scene for scene in breakdown["scenes"] if _has_art(scene, kinds) and not used & set(scene["panel_ids"])]


def _seconds(blocks: list[dict]) -> float:
    return sum(block["duration_s"] for block in blocks)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _candidate_frames(ids: list[str], breakdown: dict) -> list[str]:
    """Frame candidates for a block: its own usable panels, else the usable panels of the scene
    it narrates. A block that points at junk, or at nothing, gets none."""
    kinds = _kinds(breakdown)
    own = [i for i in dict.fromkeys(ids) if kinds.get(i) in FRAME_KINDS]
    if own or not ids or any(kinds.get(i) == "junk" for i in ids):
        return own
    scene = next((s for s in breakdown["scenes"] if set(s["panel_ids"]) & set(ids)), None)
    return [i for i in scene["panel_ids"] if kinds.get(i) in FRAME_KINDS] if scene else []


def _finish_blocks(raw: list[dict], panels: dict[str, Panel], breakdown: dict, min_frame_s: float,
                   chars_per_sec: float) -> list[dict]:
    """Apply the frame rules locally: every block keeps at least one usable frame (blocks that
    cannot get one are dropped), as many frames as fit at `min_frame_s` each (strongest kept),
    in reading order, with bubble blur boxes."""
    has_bubble = {e["id"]: e["has_bubble"] for e in breakdown["catalogue"]}
    blocks = []
    for block in raw:
        narration = block["narration"].strip()
        seconds = len(narration) / chars_per_sec
        limit = max(1, min(MAX_FRAMES, int(seconds // min_frame_s)))
        ids = sorted(_candidate_frames(block["panel_ids"], breakdown)[:limit], key=lambda i: panels[i].order)
        if not narration or not ids:
            continue
        blur = {}
        for panel_id in ids:
            if has_bubble[panel_id]:
                panel = panels[panel_id]
                boxes = [[panel.bbox[0] + x, panel.bbox[1] + y, w, h] for x, y, w, h in bubbles.find_bubbles(panel.crop)]
                if boxes:
                    blur[panel_id] = boxes
        blocks.append({"panel_ids": ids, "narration": narration, "duration_s": round(seconds, 1), "blur": blur})
    return blocks


def _write_narration(ask: Callable[[str], dict], on_event: Callable[[dict], None], *, memory: dict, ch_no: int,
                     brief: str, breakdown: dict, style: str, panels: dict[str, Panel], target_minutes: float,
                     min_frame_s: float, chars_per_sec: float) -> dict:
    """Pass 2: write the narration, re-ask once for scenes it skipped, and extend it once when
    it falls short of the target. Returns {blocks, shortfall}; a chapter that still lacks the
    material records its shortfall instead of being padded."""
    def log(line):
        on_event({"type": "log", "line": line})

    def write(prompt):
        answer = ask(prompt)
        skipped = _skipped_scenes(breakdown, answer["blocks"])
        if skipped:
            ranges = ", ".join(f"{s['panel_ids'][0]}-{s['panel_ids'][-1]}" for s in skipped)
            log(f"Narration skipped scene(s) {ranges}; asking again")
            answer = ask(f"{prompt}\n\nYour previous attempt left out the scenes at {ranges}. "
                         "Every scene must be narrated.")
            for scene in _skipped_scenes(breakdown, answer["blocks"]):
                log(f"Narration still skips {scene['panel_ids'][0]}-{scene['panel_ids'][-1]}")
        blocks = _finish_blocks(answer["blocks"], panels, breakdown, min_frame_s, chars_per_sec)
        if len(blocks) < len(answer["blocks"]):
            log(f"Dropped {len(answer['blocks']) - len(blocks)} block(s) with no usable frame")
        return blocks

    target_s = target_minutes * 60
    prompt = _narration_prompt(memory, ch_no, brief, breakdown, style, target_minutes, chars_per_sec)
    blocks = write(prompt)
    if _seconds(blocks) < target_s * LENGTH_TOLERANCE:
        log(f"Narration runs {_seconds(blocks) / 60:.1f} min against a {target_minutes:g} min target; "
            "extending it with deeper beats")
        extended = write(_extension_prompt(prompt, blocks, target_minutes))
        if _seconds(extended) > _seconds(blocks):
            blocks = extended
    shortfall = None
    if _seconds(blocks) < target_s * LENGTH_TOLERANCE:
        shortfall = {"target_minutes": target_minutes, "actual_minutes": round(_seconds(blocks) / 60, 1)}
        log(f"Narration is {shortfall['actual_minutes']} min, short of the {target_minutes:g} min target: "
            "the chapter does not have more material, so it was not padded")
    return {"blocks": blocks, "shortfall": shortfall}


def read_usage(state_path: Path) -> dict | None:
    """{pass1, pass2, total} token usage saved for the chapter, else None."""
    try:
        return read_json_with_backup_fallback(state_path).get("usage")
    except (OSError, ValueError):
        return None


def read_narration(state_path: Path) -> dict | None:
    """{blocks, saved_at} once pass 2 has completed for the chapter, else None."""
    try:
        return read_json_with_backup_fallback(state_path).get("narration")
    except (OSError, ValueError):
        return None


def run_chapter(*, panels: list[Panel], work_dir: Path, state_path: Path, memory_path: Path, ch_no: int,
                brief: str, style: str, models: dict, target_minutes: float, min_frame_s: float,
                chars_per_sec: float, on_event: Callable[[dict], None],
                on_limit: Callable[[datetime], None],
                on_proc: Callable[[subprocess.Popen], None],
                before_call: Callable[[], None] | None = None) -> dict:
    """Run (or resume) both passes for one chapter and return its narration
    ({blocks, saved_at, shortfall}).
    `on_event` receives a plan event, a step event as each stage starts, and the stream
    events of each call, all tagged with their pass (None for the story update).
    `before_call` runs before every Claude call (see claude_cli.call)."""
    try:
        state = read_json_with_backup_fallback(state_path)
    except (OSError, ValueError):
        state = {}
    if state.get("memory_applied"):
        state = {}
    memory = load_memory(memory_path)

    dropped = [p.id for p in panels if panel_split.near_uniform(p.crop)]
    sent = [p for p in panels if p.id not in dropped]
    by_id = {p.id: p for p in sent}
    on_event({"type": "plan", "sent": list(by_id), "dropped": dropped})

    def call(number, content, schema, model, usage, mention, ids, prior=None):
        def forward(event):
            if prior and event["type"] == "usage":
                event = {**event, "usage": claude_cli.sum_usage([prior, event["usage"]])}
            on_event({**event, "pass": number})

        return claude_cli.call(content, schema, model, work_dir, usage=usage, mention_event=mention,
                               panel_ids=set(ids), on_proc=on_proc, on_limit=on_limit, on_event=forward,
                               before_call=before_call)

    if "breakdown" not in state:
        batches = _batches(list(by_id))
        done = state.setdefault("batches", [])
        for number, ids in enumerate(batches, start=1):
            if number <= len(done):
                continue
            on_event({"type": "step", "pass": 1, "label": f"Reading the panels (batch {number} of {len(batches)})"})
            earlier = [b["result"] for b in done]
            prior = claude_cli.sum_usage([b["usage"] for b in done])
            usage = claude_cli.empty_usage()
            content = _batch_content([by_id[i] for i in ids], _context(memory, ch_no, brief), earlier,
                                     ch_no, number, len(batches))

            result = call(1, content, BREAKDOWN_SCHEMA, models["comprehension"], usage, "read", ids, prior)
            missing = [i for i in ids if i not in {e["id"] for e in result["catalogue"]}]
            if missing:
                on_event({"type": "log", "line": f"Pass 1 did not catalogue {len(missing)} panel(s): "
                                                  f"{', '.join(missing[:20])}"})
            done.append({"ids": ids, "result": result, "usage": usage})
            atomic_write_json(state_path, state)
        state["breakdown"] = _merge([b["result"] for b in done])
        state["usage"] = {"pass1": claude_cli.sum_usage([b["usage"] for b in done])}
        atomic_write_json(state_path, state)

    if "narration" not in state:
        on_event({"type": "step", "pass": 2, "label": "Writing the narration"})
        usage = claude_cli.empty_usage()
        narration = _write_narration(
            lambda prompt: call(2, [{"type": "text", "text": prompt}], NARRATION_SCHEMA, models["narration"],
                                usage, "cover", list(by_id)),
            on_event, memory=memory, ch_no=ch_no, brief=brief, breakdown=state["breakdown"], style=style,
            panels={p.id: p for p in sent}, target_minutes=target_minutes, min_frame_s=min_frame_s,
            chars_per_sec=chars_per_sec)
        state["narration"] = {**narration, "saved_at": _now()}
        passes = {**state["usage"], "pass2": usage}
        state["usage"] = {**passes, "total": claude_cli.sum_usage(list(passes.values()))}
        atomic_write_json(state_path, state)

    on_event({"type": "step", "pass": None, "label": "Updating the story so far"})
    breakdown = state["breakdown"]
    memory["chapters"][str(ch_no)] = breakdown["chapter_summary"]
    memory["characters"].update({c["name"]: c["description"] for c in breakdown["characters"]})
    atomic_write_json(memory_path, memory)
    state["memory_applied"] = True
    atomic_write_json(state_path, state)
    return state["narration"]

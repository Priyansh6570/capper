"""Script engine that turns a chapter's panels into narration blocks with the local
`claude` CLI.

Pass 1 sends the panel images (downscaled copies, in batches carrying the previous
breakdown forward) and returns a factual breakdown plus a per-panel catalogue whose entries
list the subjects worth showing and roughly where they sit. Pass 2 is a fresh, text-only call
that writes the narration from that breakdown and picks, per block, subjects to show, the
play mode and any flop. The exact crops, bubble handling, frame limits and flop safety are
then applied locally (see frame_crop.py); frames are never blurred. Progress and token totals are saved after each batch and pass; the
breakdown also feeds the project's story-so-far summary and character sheet.
"""
from __future__ import annotations

import base64
import collections
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
import frame_crop
import pacing
import panel_split
from atomic_io import atomic_write_json, read_json_with_backup_fallback
from manifest import Panel

SEND_MAX_SIDE = 1024        # longest side of the copy of a panel sent to the model, px
JPEG_QUALITY = 80
BATCH_PANELS = 40           # images per pass-1 call (the API allows 100 and caps request size)
FRAME_KINDS = ("character", "action", "scene", "sound_effect")
KINDS = FRAME_KINDS + ("dialogue_bubble", "junk")
MAX_FRAMES = 3
PANELS_PER_BLOCK = 2        # a scene gets at least one block for every this many story panels in it
MAX_FRAME_OVERLAP = 0.6     # a frame covering more than this share of an earlier one in its block is dropped
DEFAULT_CHARS_PER_SEC = 15  # speaking rate assumed until a project has generated audio to measure

_STRINGS = {"type": "array", "items": {"type": "string"}}


def _named(key: str) -> dict:
    return {"type": "array", "items": {
        "type": "object", "required": ["name", key],
        "properties": {"name": {"type": "string"}, key: {"type": "string"}}}}


_BOX = {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4}

_SUBJECT = {
    "type": "object", "required": ["label", "box", "has_text", "facing"],
    "properties": {"label": {"type": "string"}, "box": _BOX, "has_text": {"type": "boolean"},
                   "facing": {"type": "string", "enum": ["left", "right", "none"]}}}

BREAKDOWN_SCHEMA = {
    "type": "object",
    "required": ["catalogue", "scenes", "characters", "uncertain", "chapter_summary"],
    "properties": {
        "catalogue": {"type": "array", "items": {
            "type": "object", "required": ["id", "description", "kind", "has_bubble", "subjects"],
            "properties": {"id": {"type": "string"}, "description": {"type": "string"},
                           "kind": {"type": "string", "enum": list(KINDS)},
                           "has_bubble": {"type": "boolean"},
                           "subjects": {"type": "array", "items": _SUBJECT}}}},
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
        "type": "object", "required": ["narration", "pace", "mode", "frames"],
        "properties": {
            "narration": {"type": "string"},
            "pace": {"type": "string", "enum": list(pacing.PACES)},
            "mode": {"type": "string", "enum": ["sequence", "together"]},
            "frames": {"type": "array", "items": {
                "type": "object", "required": ["panel_id", "subject", "flop"],
                "properties": {"panel_id": {"type": "string"}, "subject": {"type": "integer"},
                               "flop": {"type": "boolean"}}}}}}}},
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
        "that is not story art), whether it contains a dialogue bubble, and its `subjects`: for every panel that is not a "
        "dialogue_bubble or junk, one to four things worth showing on their own (a face, an action, an "
        "object, the whole scene), each with a label, its `box` as fractions of the panel [left, top, right, "
        "bottom] drawn tight around the subject and leaving speech bubbles outside where the art allows, "
        "whether lettering or sound-effect text sits inside that box, and which way the main figure faces on "
        "screen (left, right, none). `scenes`: scene by scene with "
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


def _art_count(scene: dict, kinds: dict[str, str]) -> int:
    return sum(kinds.get(i) in FRAME_KINDS for i in scene["panel_ids"])


def _has_art(scene: dict, kinds: dict[str, str]) -> bool:
    return _art_count(scene, kinds) > 0


def _min_blocks(scene: dict, kinds: dict[str, str]) -> int:
    return math.ceil(_art_count(scene, kinds) / PANELS_PER_BLOCK)


def _kinds(breakdown: dict) -> dict[str, str]:
    return {e["id"]: e["kind"] for e in breakdown["catalogue"]}


def _shape(panel: Panel, box: list[float]) -> str:
    ratio = (box[2] - box[0]) * panel.bbox[2] / max(1e-6, (box[3] - box[1]) * panel.bbox[3])
    return "tall and narrow" if ratio < 0.7 else "wide" if ratio > 1.5 else "roughly square"


def _catalogue_text(breakdown: dict, panels: dict[str, Panel]) -> str:
    lines = []
    for entry in breakdown["catalogue"]:
        if entry["kind"] not in FRAME_KINDS:
            continue
        lines.append(f"{entry['id']} | {entry['kind']} | {'bubble' if entry['has_bubble'] else 'no bubble'} | "
                     f"{entry['description']}")
        for number, subject in enumerate(entry.get("subjects", [])):
            lines.append(f"    {number}: {subject['label']} ({_shape(panels[entry['id']], subject['box'])}, "
                         f"faces {subject['facing']}{', contains text' if subject['has_text'] else ''})")
    return "\n".join(lines)


def _narration_prompt(memory: dict, ch_no: int, brief: str, breakdown: dict, style: str,
                      panels: dict[str, Panel]) -> str:
    kinds = _kinds(breakdown)
    scenes = "\n".join(
        f"[{s['panel_ids'][0]}-{s['panel_ids'][-1]}] {_art_count(s, kinds)} story panels, at least "
        f"{_min_blocks(s, kinds)} blocks | {s['importance']} | {s['location']} | "
        f"{', '.join(s['characters_present'])} | {s['what_happens']} | wants: {_wants_text(s)} | "
        f"feelings: {s['feelings']} | stakes: {s['stakes']} | world: {s['world_info']}"
        for s in breakdown["scenes"] if _has_art(s, kinds))
    catalogue = _catalogue_text(breakdown, panels)
    art = sum(kind in FRAME_KINDS for kind in kinds.values())
    uncertain = "\n".join(f"{', '.join(u['panel_ids'])}: {u['note']}" for u in breakdown["uncertain"]) or "(none)"
    return (
        f"You are writing the recap narration for chapter {ch_no} of a manhwa, working only from the notes "
        f"below (you have no images).\n\n{_context(memory, ch_no, brief)}\n\n"
        f"Chapter summary:\n{breakdown['chapter_summary']}\n\nScenes, in order:\n{scenes}\n\n"
        f"Uncertain (do not resolve these):\n{uncertain}\n\n"
        f"Frame candidates (the only panels you may use), each followed by its numbered subjects:\n{catalogue}\n\n{style}\n\n"
        "Return ordered blocks, written the way a skilled recap-channel narrator tells the story to a viewer "
        "who cannot see the panels: you add understanding and energy, you do not read the panels out.\n"
        "- Start with the chapter's first scene and narrate every scene in order; none may be skipped. "
        "Never narrate title cards, logos, credits, ads or chapter-end cards, and never mention that the "
        "chapter or an episode ended.\n"
        f"- Cover the whole chapter. The notes list {art} panels with story art and every one of them belongs "
        "to a block: a block narrates one moment spanning one to three panels, and a panel that only continues "
        "the moment before it is folded into that block, never dropped. Each scene line gives its panel count "
        "and the fewest blocks it may get, so on average a block covers no more than two panels; a scene may "
        "get more. There is no target length beyond that; the story sets it. A fight or action sequence may be "
        "compressed into flowing narration, but each hit, turn, reveal and reaction that matters is still "
        "narrated.\n"
        "- A block is two to three sentences, about 25 to 50 words, in story order; turning points, reveals "
        "and emotional peaks may run a little longer. Two distinct beats are two blocks; one beat is never "
        "spread over several blocks.\n"
        "- Every block does more than say what happened. Say why it matters and what is at stake; what a "
        "character wants, feels or is thinking when the notes show it; how a rule or power of this world "
        "works when a scene shows one; a brief reminder of an earlier event from the story so far when it "
        "bears on the moment; and let tension build toward a turn and react when a reveal lands.\n"
        "- Filler is the only thing to leave out: repeating an event in other words, inventing events, "
        "motives, inner thoughts or descriptions (of a person, a dish, a place) the notes do not "
        "give, generic hype (\"you won't believe what happens next\"), and stretching one panel over lines "
        "that add nothing. Ground everything else in the notes; never invent plot, names, numbers or facts. "
        "Speak as the narrator inside the story: never mention the notes or the panels, and when something "
        "is unclear or unnamed, leave it out instead of saying it is unknown. If the narration is in the first "
        "person, \"I\" is only the protagonist; every other character is named or called he, she or a man, "
        "never \"I\".\n"
        "- Write for the ear. End each thought with a full stop and use a comma only where a speaker would "
        "pause. No dashes, ellipses, colons, semicolons or parentheses, and no quotation marks in the middle "
        "of a sentence: report speech, or end the sentence on a short quote.\n"
        "- `pace`: how the block is delivered. Most blocks are \"calm\". \"tense\" is for suspense and "
        "confrontation, \"action\" only for physical fights and fast, violent events, \"sad\" for grief, loss "
        "and quiet emotion. \"reveal\" is only for a real surprise or discovery, a few times in a chapter at "
        "most: the voice pauses before it, so put the revelation in the block's last sentence. \"comic\" is "
        "only for a real joke or comic reaction: the voice pauses before the punchline, so put it in the last "
        "sentence. \"excited\" is for joy, triumph, hype and cheering, \"angry\" for rage and shouting. Pick "
        "the pace that matches the feeling of the block, not just the plot event, and use \"calm\" when "
        "nothing stands out; a quiet or sad scene is \"sad\" or \"calm\", never \"excited\".\n"
        "- Narrate events and their meaning, never what is visible in a panel. If an object's identity is "
        "unclear, call it what the dialogue calls it, or leave it unnamed.\n"
        "- `frames`: one to three frames that best show the moment, strongest first, each a candidate panel "
        "and the number of the subject to show from it (the app crops to that subject). Prefer subjects "
        "that do not need a speech bubble in the picture.\n"
        "- `mode`: \"sequence\" by default, the frames following one another across the block. Use "
        "\"together\" only when two or three frames belong to one moment (an action and its reaction, two "
        "characters facing off), or when a subject is tall and narrow and would leave the screen empty "
        "beside it; then the frames appear at once.\n"
        "- `flop` mirrors a frame left to right. Use it rarely, only when screen direction calls for it: so "
        "two characters shown together face each other, or so movement continues the direction of the "
        "previous frame. Never flop a subject that contains text; the app ignores the flag there.")


def _thin_scenes(breakdown: dict, blocks: list[dict]) -> list[tuple[dict, int]]:
    """The scenes with story art that got fewer blocks than their panels call for (none at all, for a
    skipped scene), each with the number it got. A block belongs to the scene of its first frame."""
    kinds = _kinds(breakdown)
    scene_of = {i: number for number, scene in enumerate(breakdown["scenes"]) for i in scene["panel_ids"]}
    got = collections.Counter(scene_of.get(block["frames"][0]["panel_id"]) for block in blocks if block["frames"])
    return [(scene, got[number]) for number, scene in enumerate(breakdown["scenes"])
            if got[number] < _min_blocks(scene, kinds)]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _candidate_frames(frames: list[dict], breakdown: dict) -> list[dict]:
    """The block's picks that point at usable panels, else the usable panels of the scene it
    narrates (whole-panel, no flop). A block that points at junk, or at nothing, gets none."""
    kinds = _kinds(breakdown)
    ids = [f["panel_id"] for f in frames]
    own = [f for f in frames if kinds.get(f["panel_id"]) in FRAME_KINDS]
    if own or not ids or any(kinds.get(i) == "junk" for i in ids):
        return own
    scene = next((s for s in breakdown["scenes"] if set(s["panel_ids"]) & set(ids)), None)
    return [{"panel_id": i, "subject": -1, "flop": False} for i in scene["panel_ids"]
            if kinds.get(i) in FRAME_KINDS] if scene else []


def _rect(frame: dict) -> frame_crop.Rect:
    x, y, w, h = frame["bbox"]
    return (x, y, x + w, y + h)


def _frame(pick: dict, panel: Panel, entry: dict, bubble_boxes: list[list[int]]) -> dict:
    """The exact frame for one pick: crop and flip decided locally."""
    x, y, w, h = panel.bbox
    subjects = entry.get("subjects", [])
    subject = subjects[pick["subject"]] if 0 <= pick["subject"] < len(subjects) else None
    box = subject["box"] if subject else None
    crop = frame_crop.frame_for(
        (x, y, x + w, y + h),
        (x + box[0] * w, y + box[1] * h, x + box[2] * w, y + box[3] * h) if box else None,
        [(x + bx, y + by, x + bx + bw, y + by + bh) for bx, by, bw, bh in bubble_boxes])
    may_flop = not (subject and subject["has_text"]) and entry["kind"] != "sound_effect"
    return {"panel_id": panel.id,
            "bbox": [round(crop[0]), round(crop[1]), round(crop[2] - crop[0]), round(crop[3] - crop[1])],
            "flip": pick["flop"] and may_flop}


def _finish_blocks(raw: list[dict], panels: dict[str, Panel], breakdown: dict, min_frame_s: float,
                   chars_per_sec: float) -> list[dict]:
    """Apply the frame rules locally: every block keeps at least one usable frame (blocks that
    cannot get one are dropped); a sequence shows as many frames as fit at `min_frame_s` each
    (strongest kept) and a together block up to MAX_FRAMES; frames are in reading order and
    cropped, de-bubbled and flopped by `_frame`."""
    entries = {e["id"]: e for e in breakdown["catalogue"]}
    found: dict[str, list[list[int]]] = {}
    blocks = []
    for block in raw:
        narration = block["narration"].strip()
        seconds = len(narration) / chars_per_sec
        together = block["mode"] == "together"
        limit = MAX_FRAMES if together else max(1, min(MAX_FRAMES, int(seconds // min_frame_s)))
        picks = list({(f["panel_id"], f["subject"]): f
                      for f in _candidate_frames(block["frames"], breakdown)}.values())[:limit]
        picks.sort(key=lambda f: (panels[f["panel_id"]].order, f["subject"]))
        if not narration or not picks:
            continue
        frames = []
        for pick in picks:
            panel, entry = panels[pick["panel_id"]], entries[pick["panel_id"]]
            if entry["has_bubble"] and panel.id not in found:
                found[panel.id] = bubbles.find_bubbles(panel.crop)
            frame = _frame(pick, panel, entry, found.get(panel.id, []))
            if not any(frame_crop.overlap_share(_rect(frame), _rect(earlier)) > MAX_FRAME_OVERLAP for earlier in frames):
                frames.append(frame)
        blocks.append({"narration": narration, "duration_s": round(seconds, 1),
                       "pace": block["pace"] if block["pace"] in pacing.PACES else pacing.DEFAULT_PACE,
                       "mode": "together" if together and len(frames) > 1 else "sequence", "frames": frames})
    return blocks


def _write_narration(ask: Callable[[str], dict], on_event: Callable[[dict], None], *, memory: dict, ch_no: int,
                     brief: str, breakdown: dict, style: str, panels: dict[str, Panel], min_frame_s: float,
                     chars_per_sec: float) -> list[dict]:
    """Pass 2: write the narration, re-asking once when a scene got fewer blocks than its panels call
    for (coverage, not length: it is never padded, extended or trimmed afterwards)."""
    def log(line):
        on_event({"type": "log", "line": line})

    def describe(thin: list[tuple[dict, int]]) -> str:
        kinds = _kinds(breakdown)
        return "; ".join(f"{s['panel_ids'][0]}-{s['panel_ids'][-1]} has {got} block(s) and needs at least "
                         f"{_min_blocks(s, kinds)}" for s, got in thin)

    prompt = _narration_prompt(memory, ch_no, brief, breakdown, style, panels)
    answer = ask(prompt)
    thin = _thin_scenes(breakdown, answer["blocks"])
    if thin:
        log(f"Narration is thin in {len(thin)} scene(s): {describe(thin)}; asking again")
        answer = ask(f"{prompt}\n\nYour previous attempt covered the story too thinly: {describe(thin)}. "
                     "Write the complete narration again, giving every scene at least the blocks its line "
                     "asks for. Do not shorten blocks to make room: each stays 25 to 50 words with its context, "
                     "stakes and feeling, so split the beats finer and give every panel its due.")
        thin = _thin_scenes(breakdown, answer["blocks"])
        if thin:
            log(f"Narration is still thin: {describe(thin)}")
    blocks = _finish_blocks(answer["blocks"], panels, breakdown, min_frame_s, chars_per_sec)
    if len(blocks) < len(answer["blocks"]):
        log(f"Dropped {len(answer['blocks']) - len(blocks)} block(s) with no usable frame")
    return blocks


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
                brief: str, style: str, models: dict, min_frame_s: float,
                chars_per_sec: float, on_event: Callable[[dict], None],
                on_limit: Callable[[datetime], None],
                on_proc: Callable[[subprocess.Popen], None],
                before_call: Callable[[], None] | None = None) -> dict:
    """Run (or resume) both passes for one chapter and return its narration
    ({blocks, saved_at}).
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
        blocks = _write_narration(
            lambda prompt: call(2, [{"type": "text", "text": prompt}], NARRATION_SCHEMA, models["narration"],
                                usage, "cover", list(by_id)),
            on_event, memory=memory, ch_no=ch_no, brief=brief, breakdown=state["breakdown"], style=style,
            panels={p.id: p for p in sent}, min_frame_s=min_frame_s, chars_per_sec=chars_per_sec)
        state["narration"] = {"blocks": blocks, "saved_at": _now()}
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

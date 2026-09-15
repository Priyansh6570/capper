"""Stage 5 / STAGE A - Story-first recap script.  Runs on: Groq or local Ollama.

The pipeline is STORY-FIRST. Earlier the frames were chosen first and narrated
per-fragment, so the script could never follow the story. Now this stage writes
the WHOLE recap script up front, from ALL of the chapter's OCR text, and a later
stage (s2b, STAGE B) matches a character frame to each piece of that script.

What it does, in ONE `llm.chat_json` call:
  1. Reads EVERY candidate panel's OCR (the full stage-2 output, ~141 panels,
     NOT the cropped selection) in reading order - each panel's `order` + its
     cleaned dialogue. Obvious OCR garble is cleaned first (`_clean_ocr`).
  2. Adds the series summary (`series/<active>/story.md`) as story ground-truth,
     plus an optional per-chapter note and a tone persona.
  3. Instructs the model to FIRST understand the whole chapter as one continuous
     story, THEN write the full recap told from the HERO'S PERSPECTIVE (the
     protagonist's POV / journey, not a neutral observer), as flowing narration
     in the series' tone - clean prose only, no raw OCR.
  4. The script is returned as ordered SEGMENTS:
        {"segments":[{"seq":int,"text":str,"refers_to_orders":[int,...]}, ...]}
     where `refers_to_orders` are the input panel order numbers whose content /
     dialogue that segment is describing (the model's best guess of which part of
     the chapter each segment covers). STAGE B uses those to place frames.

Outputs (no panels touched here):
  - work/<chapter>/script/segments.json - the structured segments (STAGE B reads).
  - work/<chapter>/script/script.txt    - the continuous prose, for review.

Bookends: an intro hook is prepended to the first segment and a closing CTA
appended to the last, so the recap opens by framing the story (and inviting a
subscribe) and closes with a call to action. They are generated from the series
summary when present, else safe defaults.

The backend (Groq cloud or local Ollama, e.g. qwen2.5:7b) is chosen by
`MANHWA_LLM`; see llm.py. For Groq, reads `GROQ_API_KEY` from `.env`.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv

from config import ROOT
from llm import chat_json
from manifest import Manifest

# Target narration style. The model must match this voice and rhythm exactly.
TARGET_STYLE = """\
The story begins in the chaotic Warring States period, a time of endless
slaughter. The great Qin, under the guise of being the strongest state of the
era, has begun to hatch a plot to eliminate the remaining six states in
preparation for unifying the world. Yong Shang, one of Qin's key military
bases, holds the most elite army in the entire state.

The scene shifts to the Li family village, nestled behind the mountains near
Yong Shang. There, a young man kneels before a grave. A young woman, her voice
heavy with concern, says, "Chi-gege, Annie has passed away," and tries to pull
him to his feet in the pouring rain. But the young man stubbornly replies,
"Yuoner, you go back. I'm fine.\""""

# Narrator personas. Each is a complete attitude brief, not an adjective swap.
TONE_PERSONAS = {
    "serious": """\
Your tone is SERIOUS: grave, weighty, and restrained. You carry the story with
the steady authority of a narrator who knows how much is at stake. Preserve every
ounce of tension - never undercut a dramatic moment, never wink at the audience.
Let dread, resolve, and consequence land with their full weight. Your sentences
are measured and deliberate; silence and stillness are ominous, not empty. When a
character suffers or a threat looms, you hold the gravity rather than rushing past
it.""",
    "comedy": """\
Your tone is COMEDY: you are actively, deliberately funny. Crack jokes, land puns,
toss in absurd asides and playful exaggeration, and riff on how ridiculous a
moment is. You are perfectly willing to break tension for a laugh - deflate a
dramatic stare, mock a villain's monologue, narrate a brooding silence like a
sports commentator. Wink at the audience. BUT you still follow the actual story
faithfully: the events, characters, and outcomes stay true; only your delivery is
comic. Be witty, not random - the humor rides on top of what genuinely happens.""",
    "sad": """\
Your tone is SAD: melancholy, tender, and emotionally deep. You linger on loss,
longing, and the ache beneath each moment. Find the sorrow even in quiet beats -
what was taken, what can't be undone, what a character feels but cannot say. Let
grief breathe; dwell on feeling rather than racing through plot. Your language is
soft and mournful, drawn to fading light, rain, distance, and goodbyes. Honor the
weight of suffering without melodrama - the heartbreak is in the restraint and the
detail, not in exclamation.""",
}
DEFAULT_TONE = "serious"

# Bookend lines that frame the recap. The intro is prepended to the FIRST segment
# (so it plays over the first frame) and the closing appended to the LAST. When a
# series brief exists they are generated from it; otherwise these defaults are used.
INTRO_FALLBACK = ("Here is a recap of this chapter - if you enjoy it, subscribe "
                  "for more.")
CLOSING_FALLBACK = ("If you enjoyed this recap, subscribe and stay tuned for the "
                    "next chapter.")


def _resolve_tone(m: Manifest) -> str:
    """Tone from a manifest field if the orchestrator threads one, else env var."""
    tone = getattr(m, "tone", None) or os.environ.get("MANHWA_TONE") or DEFAULT_TONE
    tone = tone.strip().lower()
    if tone not in TONE_PERSONAS:
        print(f"  unknown tone {tone!r}; falling back to {DEFAULT_TONE!r}")
        tone = DEFAULT_TONE
    return tone


def _load_series_summary() -> tuple[str, str]:
    """Load the standing series brief from `series/<active>/story.md`.

    The active series name lives in `series/active.txt`. Returns `(name, summary)`;
    either may be "" if the file is missing/empty.
    """
    active_path = ROOT / "series" / "active.txt"
    if not active_path.exists():
        return "", ""
    name = active_path.read_text(encoding="utf-8").strip()
    if not name:
        return "", ""
    story_path = ROOT / "series" / name / "story.md"
    if not story_path.exists():
        return name, ""
    return name, story_path.read_text(encoding="utf-8").strip()


def _load_chapter_note(m: Manifest) -> str:
    """Load the optional per-chapter ground-truth note from note.txt."""
    path = Path(m.work_dir) / "note.txt"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------- #
# OCR hygiene + prose validation.
# --------------------------------------------------------------------------- #
def _clean_ocr(text: str | None) -> str:
    """Make raw OCR readable BEFORE the model sees it: join "/"-split fragments,
    drop stray artifacts, de-shout ALL-CAPS, and fix the common OCR confusions -
    grouped numbers ("IO,OOO" -> "10,000") and weird internal capitals ("EviL" ->
    "evil"). This only TIDIES text; it does not write prose."""
    if not text:
        return ""
    t = str(text).strip()
    t = re.sub(r"\s*/\s*", ", ", t)                      # "/" line-joins -> commas
    t = t.replace("_", " ")                             # stray underscores
    t = re.sub(r"[{}\[\]|#~^<>*=\\@]", " ", t)            # layout/markup junk -> space
    t = re.sub(r"([!?.,*=~-])\1{2,}", r"\1", t)          # collapse punctuation runs
    t = re.sub(
        r"\b[0-9IlO]{1,3}(?:,[0-9IlO]{3})+\b",
        lambda m: m.group(0).replace("O", "0").replace("I", "1").replace("l", "1"),
        t,
    )
    t = re.sub(
        r"\b[A-Za-z]+\b",
        lambda m: m.group(0).lower() if re.search(r"[a-z][A-Z]", m.group(0)) else m.group(0),
        t,
    )
    t = re.sub(r"\b[A-Z]{3,}\b", lambda m: m.group(0).capitalize(), t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# Characters that betray raw OCR / layout garble - they never belong in prose.
_OCR_MARKER_RE = re.compile(r"[\\/_{}\[\]|#~^<>*=:@]")


def _looks_like_ocr(text: str | None) -> bool:
    """True if a script line is blank or still looks like raw OCR rather than
    written prose (markers, punctuation spew, ALL-CAPS dumps, garble tokens)."""
    n = (text or "").strip()
    if not re.search(r"[A-Za-z0-9]", n):
        return True
    if _OCR_MARKER_RE.search(n):
        return True
    if re.search(r"([!?.,;*=~-])\1{2,}", n):
        return True
    if re.search(r"\b[A-Z]{2,}\b(?:[^A-Za-z]+\b[A-Z]{2,}\b)+", n):
        return True
    words = re.findall(r"[A-Za-z]{2,}", n)
    if words and sum(w.isupper() for w in words) / len(words) >= 0.4:
        return True
    for w in words:
        if re.search(r"[a-z][A-Z]", w):
            return True
        if w.isupper() and len(w) >= 4:
            return True
    return False


def _strip_layout_markers(text: str) -> str:
    """Remove stray OCR/layout markers from model prose without altering casing,
    so a clean segment never carries a slash/brace/bracket through to the script."""
    t = re.sub(r"[\\/_{}\[\]|#~^<>*=]", " ", text or "")
    return re.sub(r"\s+", " ", t).strip()


# --------------------------------------------------------------------------- #
# Bookends (intro hook + closing CTA).
# --------------------------------------------------------------------------- #
def _context_block(series_summary: str, chapter_note: str) -> str:
    """The authoritative series/chapter context shared by the prompts."""
    block = ""
    if series_summary:
        block += f"""
AUTHORITATIVE SERIES BACKGROUND:
The following standing brief for this series was written by the user. Trust it
over the OCR for character NAMES, ROLES, RELATIONSHIPS, and the SETTING, and use
it to know WHO THE HERO is so you can tell the story from their perspective.
\"\"\"
{series_summary}
\"\"\"
"""
    if chapter_note:
        block += f"""
AUTHORITATIVE CHAPTER GROUND-TRUTH:
The following note describes what actually happens in THIS chapter, written by the
user. Trust it over the noisy OCR whenever they conflict.
\"\"\"
{chapter_note}
\"\"\"
"""
    return block


def _sanitize_bookend(text: str, fallback: str) -> str:
    """A bookend must be a clean single-line prose sentence."""
    t = _strip_layout_markers(text or "").strip(" -")
    return t if re.search(r"[A-Za-z]", t) else fallback


def _generate_bookends(tone: str, series_summary: str) -> tuple[str, str]:
    """Return (intro_hook, closing_cta) in the narrator's tone, generated from the
    series brief when available, with safe static fallbacks."""
    if not series_summary:
        return INTRO_FALLBACK, CLOSING_FALLBACK
    persona = TONE_PERSONAS[tone]
    system = f"""You write the opening HOOK and closing CALL-TO-ACTION for a
manhwa/manga recap video, in this narrator's voice.

{persona}

Using the series background, write two short lines:
- "intro": ONE punchy sentence that frames what the WHOLE story is about, from the
  hero's angle, and invites the viewer to subscribe - e.g. "This is the story of a
  man who returned through time to take his revenge; if you enjoy it, subscribe
  for more."
- "closing": ONE short sentence thanking the viewer and inviting them to subscribe
  and come back for the next chapter.
Plain prose, present tense, no emojis, no markdown, no surrounding quotes.
Return ONLY JSON: {{"intro": "<string>", "closing": "<string>"}}."""
    user = (f'SERIES BACKGROUND:\n"""\n{series_summary}\n"""\n\n'
            f"Write the intro hook and closing call-to-action now.")
    try:
        data = chat_json(system, user, max_tokens=400, temperature=0.8)
    except Exception as e:                              # noqa: BLE001 - non-fatal
        print(f"  bookend generation failed ({type(e).__name__}: {e}); using defaults")
        return INTRO_FALLBACK, CLOSING_FALLBACK
    intro = _sanitize_bookend(data.get("intro") or "", INTRO_FALLBACK)
    closing = _sanitize_bookend(data.get("closing") or "", CLOSING_FALLBACK)
    return intro, closing


# --------------------------------------------------------------------------- #
# Story-first script prompt.
# --------------------------------------------------------------------------- #
def _build_system_prompt(tone: str, series_summary: str, chapter_note: str) -> str:
    persona = TONE_PERSONAS[tone]
    context_block = _context_block(series_summary, chapter_note)
    return f"""You are the scriptwriter and narrator of a manhwa/manga recap video.
You will receive the on-page TEXT (noisy OCR) of EVERY panel of one chapter, in
reading order, each labelled with its panel ORDER number.

{persona}
{context_block}
FIRST, silently read the whole chapter and understand it as ONE continuous story -
who the hero is, what they want, what happens to them, and how the chapter moves.
The OCR is noisy: fragmented, garbled, sometimes duplicated. Infer meaning from
the surrounding panels and the series background; never quote raw OCR garbage,
quote what the character clearly meant. Do not output this analysis.

THEN WRITE THE FULL RECAP SCRIPT, told from the HERO'S PERSPECTIVE - follow the
protagonist's point of view and journey through the chapter, not a detached
observer's. It must read as one flowing, continuous story from start to finish in
your tone. Present tense. Clean prose only - NO raw OCR, no slashes, no ALL-CAPS
dumps, no panel numbers inside the prose. Embed dialogue as natural attributed
quotes (e.g. he says, "...").

Break the script into ORDERED SEGMENTS in reading order. Each segment is one to
three sentences that will sit under a SINGLE frame in the video, so keep each
segment to one clear moment. For EACH segment also give "refers_to_orders": the
panel ORDER numbers from the input whose content/dialogue that segment is
describing (your best guess of which part of the chapter it covers - usually one
to a few consecutive orders). Cover the chapter start to finish; aim for roughly
one segment per real story beat (a couple dozen for a typical chapter).

Match the prose QUALITY of this TARGET STYLE (its voice control, rhythm, and use
of attributed quotes) under whatever tone you were given:
\"\"\"
{TARGET_STYLE}
\"\"\"

Return ONLY valid JSON, no prose around it, in exactly this shape:
{{"segments": [{{"seq": <int>, "text": "<string>", "refers_to_orders": [<int>, ...]}}, ...]}}
Number `seq` from 1 in reading order. Use only panel order numbers that appear in
the input."""


def _build_user_message(text_panels: list[tuple[int, str]]) -> str:
    """Render every panel's order + cleaned OCR text in reading order."""
    blocks = [
        f"Here is the on-page text of all {len(text_panels)} panels with text, in "
        f"reading order. Format: [order] text.\n"
    ]
    for order, text in text_panels:
        blocks.append(f"[{order}] {text}")
    blocks.append(
        "\nNow write the full hero-POV recap script as ordered segments and "
        "return the JSON object."
    )
    return "\n".join(blocks)


def _parse_segments(data: dict, valid_orders: set[int]) -> list[dict]:
    """Turn the model reply into clean, ordered segments. Each kept segment has a
    prose `text` (markers stripped, non-blank) and `refers_to_orders` filtered to
    real candidate orders. `seq` is renumbered 1..N in returned order."""
    out: list[dict] = []
    for item in data.get("segments", []):
        if not isinstance(item, dict):
            continue
        text = _strip_layout_markers(str(item.get("text") or ""))
        if not re.search(r"[A-Za-z]", text):
            continue
        refers = []
        for o in item.get("refers_to_orders") or []:
            try:
                oi = int(o)
            except (TypeError, ValueError):
                continue
            if oi in valid_orders:
                refers.append(oi)
        out.append({"seq": len(out) + 1, "text": text,
                    "refers_to_orders": sorted(set(refers))})
    return out


# --------------------------------------------------------------------------- #
def run(m: Manifest) -> Manifest:
    load_dotenv()  # load .env early so MANHWA_TONE / MANHWA_LLM are visible here

    out_dir = Path(m.work_dir) / "script"
    out_dir.mkdir(parents=True, exist_ok=True)

    tone = _resolve_tone(m)
    series_name, series_summary = _load_series_summary()
    chapter_note = _load_chapter_note(m)

    sources = []
    if series_summary:
        sources.append(f"series brief '{series_name}' ({len(series_summary)} chars)")
    if chapter_note:
        sources.append(f"chapter note ({len(chapter_note)} chars)")
    print(f"  context loaded: {', '.join(sources)} - using as ground-truth"
          if sources else
          "  no story context (no series brief or note.txt); OCR only")
    print(f"  narrative tone: {tone}")

    # ALL candidate panels (the full stage-2 output), in reading order. Pull each
    # panel's order + CLEANED OCR; keep only those that actually carry text.
    panels = m.ordered_panels()
    text_panels = [(p.order, _clean_ocr(p.dialogue)) for p in panels]
    text_panels = [(o, t) for (o, t) in text_panels if t]
    valid_orders = {o for (o, _t) in text_panels}
    print(f"  read OCR from {len(text_panels)} of {len(panels)} candidate panels")

    if not text_panels:
        (out_dir / "script.txt").write_text("", encoding="utf-8")
        (out_dir / "segments.json").write_text(
            json.dumps({"segments": []}, indent=2), encoding="utf-8")
        print("  no OCR text to script from")
        m.stage = "script"
        return m

    system_prompt = _build_system_prompt(tone, series_summary, chapter_note)
    user_message = _build_user_message(text_panels)
    data = chat_json(system_prompt, user_message, max_tokens=8000, temperature=0.7)

    segments = _parse_segments(data, valid_orders)
    if not segments:
        print("  WARNING: model returned no usable segments")
        (out_dir / "script.txt").write_text("", encoding="utf-8")
        (out_dir / "segments.json").write_text(
            json.dumps({"segments": []}, indent=2), encoding="utf-8")
        m.stage = "script"
        return m

    # BOOKENDS - frame the recap with an intro hook (first segment) and a closing
    # call-to-action (last segment).
    intro, closing = _generate_bookends(tone, series_summary)
    segments[0]["text"] = f"{intro} {segments[0]['text']}".strip()
    segments[-1]["text"] = f"{segments[-1]['text']} {closing}".strip()

    # segments.json drives STAGE B; script.txt is the continuous prose for review.
    (out_dir / "segments.json").write_text(
        json.dumps({"chapter_id": m.chapter_id, "segments": segments},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "script.txt").write_text(
        "\n\n".join(s["text"] for s in segments) + "\n", encoding="utf-8")

    n_refs = sum(1 for s in segments if s["refers_to_orders"])
    print(f"  wrote {len(segments)} script segments "
          f"({n_refs} with panel refs) -> {out_dir / 'segments.json'}")
    print(f"  full script -> {out_dir / 'script.txt'}")
    m.stage = "script"
    return m

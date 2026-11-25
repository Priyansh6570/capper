"""The quote bank for the quote short (style 6): a project's own list of lines, editable by hand.

A quote is {"id", "text", "mood", "emphasis": [words], "character", "accent": "#rrggbb" | None}. They live in one JSON file
({"quotes": [...]}), normally `projects/<slug>/quotes.json`; `suggest()` asks Claude for philosophical lines the
protagonist says (the user then edits, deletes or adds their own; nothing here is shown without them picking one).
`mood` is a pace name or a look name from `backdrop.EMOTION_LOOKS` and drives the accent colour, the backdrop and
which frames of the protagonist are used.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MOODS = ("calm", "sad", "tense", "action", "reveal", "cold", "nostalgic")
# accent per mood, BGR (what the renderer uses); bright enough to read over a dimmed picture
ACCENT_BGR = {
    "calm": (170, 225, 175),       # pale jade
    "sad": (255, 200, 140),        # ice blue
    "tense": (80, 110, 255),       # hot red
    "action": (60, 150, 255),      # ember orange
    "reveal": (90, 210, 255),      # gold
    "cold": (255, 225, 175),       # frost
    "nostalgic": (110, 180, 240),  # amber
}
STOP = set("""a an the and or but if so as of to in on at by for with from into onto up down out over under again then than that
this these those is are was were be been being am do does did done have has had having not no nor only just very can could
should would will shall may might must it its it's i me my mine you your yours he him his she her hers we us our ours they
them their theirs who whom whose which what when where why how there here all any both each few more most other some such
own same too s t don doesn didn isn aren wasn weren won wouldn couldn shouldn""".split())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "quote"


def load(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for q in data.get("quotes", []) if isinstance(data, dict) else []:
        if isinstance(q, dict) and str(q.get("text", "")).strip():
            out.append(normalise(q))
    return out


def normalise(q: dict) -> dict:
    text = " ".join(str(q["text"]).split())
    mood = str(q.get("mood") or "calm").lower()
    return {"id": q.get("id") or _slug(text), "text": text, "mood": mood if mood else "calm",
            "emphasis": [str(w) for w in q.get("emphasis", []) if str(w).strip()],
            "character": q.get("character") or "", "accent": q.get("accent") or None,
            "note": q.get("note") or "", "font": q.get("font") or ""}


def save(path: str | Path, quotes: list[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"quotes": [normalise(q) for q in quotes]}, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def upsert(path: str | Path, quote: dict) -> dict:
    """Add a quote (your own line) or replace the one with the same id; returns the stored quote."""
    q = normalise(quote)
    quotes = load(path)
    for i, old in enumerate(quotes):
        if old["id"] == q["id"]:
            quotes[i] = q
            break
    else:
        quotes.append(q)
    save(path, quotes)
    return q


def delete(path: str | Path, quote_id: str) -> bool:
    quotes = load(path)
    kept = [q for q in quotes if q["id"] != quote_id]
    if len(kept) == len(quotes):
        return False
    save(path, kept)
    return True


def accent_bgr(q: dict) -> tuple[int, int, int]:
    """The quote's own colour (`accent` "#rrggbb", e.g. the series theme) or the mood's."""
    c = (q.get("accent") or "").lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", c):
        return (int(c[4:6], 16), int(c[2:4], 16), int(c[0:2], 16))
    return ACCENT_BGR.get(q.get("mood", "calm"), ACCENT_BGR["calm"])


def emphasis_words(q: dict, limit: int = 3) -> set[str]:
    """Lower-case words to colour: the quote's own `emphasis`, else the longest content words (spread over the line)."""
    own = {re.sub(r"\W", "", w).lower() for phrase in q.get("emphasis", []) for w in str(phrase).split()}
    own.discard("")
    if own:
        return own
    words = [re.sub(r"[^\w']", "", w).lower() for w in q["text"].split()]
    cand = [(len(w), -i, w) for i, w in enumerate(words) if w and w not in STOP and len(w) > 3]
    best = sorted(cand, reverse=True)[:limit]
    return {w for _, _, w in best}


# ── suggestions from Claude ──
SCHEMA = {"type": "object", "required": ["quotes"], "properties": {"quotes": {"type": "array", "items": {
    "type": "object", "required": ["text", "mood", "emphasis", "confidence"],
    "properties": {"text": {"type": "string"}, "mood": {"type": "string", "enum": list(MOODS)},
                   "emphasis": {"type": "array", "items": {"type": "string"}},
                   "confidence": {"type": "string", "enum": ["verbatim", "close", "in-spirit"]},
                   "note": {"type": "string"}}}}}}


def suggest(series: str, character: str, description: str = "", n: int = 8, model: str = "sonnet") -> list[dict]:
    """Ask Claude for `n` philosophical lines `character` says in `series`, which stand alone for someone who has not read
    it. `confidence` says how sure it is of the wording (verbatim | close | in-spirit); the app shows it so you can
    check or rewrite a line before using it. Returned quotes are not saved."""
    sys.path.insert(0, str(ROOT / "tools" / "framer"))
    sys.path.insert(0, str(ROOT))
    import claude_cli

    prompt = (
        f"Series: {series}\nCharacter: {character}\n{('About: ' + description) if description else ''}\n\n"
        f"List {n} famous or iconic lines this character says (or very close to what they say) that are philosophical: about "
        "will, regret, loyalty, power, loneliness, fate, honour or resolve, and that make complete sense to someone who "
        "has never read the series (no names of places or techniques, no plot references). English, one to three short "
        "sentences each, 12-32 words. For each: `mood` (calm, sad, tense, action, reveal, cold or nostalgic), `emphasis` the "
        "1-3 words that carry the idea, `confidence` how sure you are of the exact wording (verbatim: you know it; close: "
        "you know the line but not every word; in-spirit: written in their voice, not an actual line), and a short `note` "
        "saying where it is said if you know. Never present an invented line as verbatim."
    )
    usage = claude_cli.empty_usage()
    res = claude_cli.call([{"type": "text", "text": prompt}], SCHEMA, model, ROOT, usage=usage, mention_event="read",
                          panel_ids=set(), on_proc=lambda proc: None, on_event=lambda e: None,
                          on_limit=lambda when: (_ for _ in ()).throw(RuntimeError(f"usage limit until {when}")))
    out = []
    for q in res.get("quotes", []):
        d = normalise({**q, "character": character})
        d["note"] = f"[{q.get('confidence', '?')}] {q.get('note', '')}".strip()
        out.append(d)
    return out

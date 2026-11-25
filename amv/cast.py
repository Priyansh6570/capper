"""Which tagged frames show the protagonist.

The frame tagger describes a character a little differently in every batch ("brown-haired young man in brown cloak",
"Protagonist, brown-grey hair, cloak" ...), so counting equal strings does not find him. One text-only Claude call is
given the distinct labels (with how many frames each has) and says which ones are the same character as the protagonist;
the answer is cached by the label set, so a bank is only asked about once. Nothing here reads an image.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache" / "amv_cast"

SCHEMA = {"type": "object", "required": ["labels"], "properties": {
    "labels": {"type": "array", "items": {"type": "string"}},
    "name": {"type": "string"}}}


def label_counts(items: list[dict]) -> dict[str, int]:
    count: dict[str, int] = {}
    for x in items:
        t = x["tag"]
        lab = (t.get("character") or "").strip()
        if lab and t.get("has_face"):
            count[lab] = count.get(lab, 0) + 1
    return count


def _key(counts: dict[str, int], series: str, character: str) -> str:
    return hashlib.sha1(json.dumps([sorted(counts.items()), series, character]).encode()).hexdigest()[:20]


def protagonist_name(items: list[dict], series: str = "", log=print) -> str:
    """The protagonist's name as Claude read it from the tag descriptions ("" when it could not tell: a description)."""
    labels = protagonist_labels(items, series, "", log=log)
    counts = label_counts(items)
    try:
        name = json.loads((CACHE / f"{_key(counts, series, '')}.json").read_text(encoding="utf-8")).get("name", "")
    except (OSError, ValueError):
        name = ""
    return name if labels and name and len(name.split()) <= 4 and name[:1].isupper() else ""


def protagonist_labels(items: list[dict], series: str = "", character: str = "", model: str = "sonnet",
                       log=print) -> set[str]:
    """The labels (exact strings from the tags) that are `character`, or when none is named the main character (the one
    with the most frames). Empty set if the bank has no labelled faces."""
    counts = label_counts(items)
    if not counts:
        return set()
    key = _key(counts, series, character)
    f = CACHE / f"{key}.json"
    if f.exists():
        try:
            return set(json.loads(f.read_text(encoding="utf-8"))["labels"])
        except (OSError, ValueError, KeyError):
            pass
    sys.path.insert(0, str(ROOT / "tools" / "framer"))
    sys.path.insert(0, str(ROOT))
    import claude_cli

    who = f"the protagonist, {character}," if character else "the protagonist (the main character, who appears in most frames)"
    listing = "\n".join(f"{n}x  {lab}" for lab, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    prompt = (
        f"These are descriptions of characters from {series or 'a manhwa'}, written separately for each of many frames, so one "
        f"person has several descriptions. Each line is `<frames> x <description>`.\n\n{listing}\n\n"
        f"Return `labels`: every description above (copied exactly, without the count) that is very likely {who} by the "
        "same hair, clothes and age, including descriptions that mention fewer details. Leave out other characters and "
        "anything you are unsure of. `name`: the character's name if the descriptions tell it, else a short description."
    )
    usage = claude_cli.empty_usage()
    res = claude_cli.call([{"type": "text", "text": prompt}], SCHEMA, model, ROOT, usage=usage, mention_event="read",
                          panel_ids=set(), on_proc=lambda proc: None, on_event=lambda e: None,
                          on_limit=lambda when: (_ for _ in ()).throw(RuntimeError(f"usage limit until {when}")))
    labels = [l for l in res.get("labels", []) if l in counts]
    log(f"protagonist: {res.get('name', '?')} - {len(labels)} descriptions, {sum(counts[l] for l in labels)} face frames")
    CACHE.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"labels": labels, "name": res.get("name", "")}), encoding="utf-8")
    return set(labels)

"""Ratings and notes on built shorts, kept per project in `short_feedback.json` (beside batch.json, local to this PC).

Each entry is one short the user rated: {n, id, rating 1-5, tags, note, style, track, title, at}. They are used twice:
  * automatically - in the "Auto mix" a style or a track whose shorts keep getting 1-2 stars is left out of the
    rotation (see `avoid`), and a style/track the user picks by hand is never filtered;
  * by hand - the notes and tags are the user's wishes for the next shorts; they are shown back on the Shorts page
    and read by whoever changes the renderer. Free text cannot change a render by itself.
Nothing here touches the manifest, the orchestrator or any stage."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import projects
import shorts
from atomic_io import atomic_write_json

TAGS = ("too fast", "too slow", "boring frames", "repetitive", "wrong music", "bad drop sync", "bad thumbnail",
        "no girl", "text/lettering", "blurry", "wrong style", "bad title")
MIN_RATINGS = 2          # a style/track needs this many ratings before it can be left out
AVOID_BELOW = 2.5        # ... and an average under this
_LOCK = threading.Lock()


class FeedbackError(ValueError):
    pass


def _path(slug: str) -> Path:
    return projects.local_dir(slug) / "short_feedback.json"


def load(slug: str) -> list[dict]:
    try:
        return json.loads(_path(slug).read_text(encoding="utf-8"))["ratings"]
    except (OSError, ValueError, KeyError):
        return []


def by_short(slug: str) -> dict[str, dict]:
    return {f"{r['n']}:{r['id']}": r for r in load(slug)}


def rate(slug: str, n: int, short_id: int, rating: int, tags=None, note: str = "") -> dict:
    """Save (or replace) the rating of short `short_id` of chapter `n`."""
    if not 1 <= int(rating) <= 5:
        raise FeedbackError("A rating is 1 to 5 stars.")
    made = next((s for s in shorts.rendered(projects.chapter_output_path(slug, n)) if s["id"] == short_id), None)
    if made is None:
        raise FeedbackError("No such short.")
    entry = {"n": n, "id": short_id, "rating": int(rating),
             "tags": [t for t in (tags or []) if t in TAGS], "note": str(note or "").strip()[:600],
             "style": made.get("style"), "track": made.get("track"), "title": made.get("title"),
             "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with _LOCK:
        kept = [r for r in load(slug) if not (r["n"] == n and r["id"] == short_id)]
        atomic_write_json(_path(slug), {"ratings": kept + [entry]})
    return entry


def forget(slug: str, n: int, short_id: int | None = None) -> None:
    """Drop the ratings of a deleted short (or of all of a chapter's shorts)."""
    with _LOCK:
        rows = load(slug)
        kept = [r for r in rows if not (r["n"] == n and (short_id is None or r["id"] == short_id))]
        if len(kept) != len(rows):
            atomic_write_json(_path(slug), {"ratings": kept})


def _averages(rows: list[dict], key: str) -> dict[str, tuple[float, int]]:
    sums: dict[str, list[int]] = {}
    for r in rows:
        if r.get(key):
            sums.setdefault(r[key], []).append(r["rating"])
    return {k: (sum(v) / len(v), len(v)) for k, v in sums.items()}


def avoid(slug: str) -> dict[str, set[str]]:
    """{"styles": {...}, "tracks": {...}} the Auto mix should leave out: rated at least MIN_RATINGS times, on average
    under AVOID_BELOW stars."""
    rows = load(slug)
    return {kind: {k for k, (avg, count) in _averages(rows, field).items() if count >= MIN_RATINGS and avg < AVOID_BELOW}
            for kind, field in (("styles", "style"), ("tracks", "track"))}


def summary(slug: str) -> dict:
    """For the page: how styles and tracks are rated, what is being left out, and the latest notes."""
    rows = load(slug)
    left_out = avoid(slug)
    return {"styles": {k: {"avg": round(a, 1), "count": c} for k, (a, c) in _averages(rows, "style").items()},
            "tracks": {k: {"avg": round(a, 1), "count": c} for k, (a, c) in _averages(rows, "track").items()},
            "avoid": {k: sorted(v) for k, v in left_out.items()},
            "notes": [{"n": r["n"], "id": r["id"], "rating": r["rating"], "tags": r["tags"], "note": r["note"]}
                      for r in sorted(rows, key=lambda r: r["at"], reverse=True) if r["note"] or r["tags"]][:12]}

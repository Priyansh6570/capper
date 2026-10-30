"""The upload queue: every full video and short on its way to YouTube, kept in publishing/queue.json.

Status of an item: waiting_approval (a full video, until approved) -> queued (ready; also while it
waits for the quota or for a retry) / scheduled (a short with a slot in the future) -> uploading ->
private / live; or failed, skipped, removed (deleted from YouTube since)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from publishing import store

FILE = "queue.json"
FULL, SHORT = "full", "short"
EDITABLE = ("waiting_approval", "queued", "scheduled", "failed")
UPLOADED = ("private", "live")
UNSENT = ("waiting_approval", "queued", "scheduled", "failed", "skipped")
_progress: dict[str, float] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def all_items() -> list[dict]:
    return store.read(FILE, [])


def get(item_id: str) -> dict | None:
    return next((i for i in all_items() if i["id"] == item_id), None)


def add(fields: dict) -> dict:
    item = {"id": uuid.uuid4().hex[:12], "status": "queued", "description": "", "tags": [], "thumbnail": None,
            "attempts": 0, "next_attempt_at": None, "error": None, "warnings": [], "video_id": None,
            "session_url": None, "locked": False, "created_at": now_iso(), "uploaded_at": None, **fields}
    with store.LOCK:
        store.write(FILE, [*all_items(), item])
    return item


def update(item_id: str, **fields) -> dict | None:
    with store.LOCK:
        items = all_items()
        item = next((i for i in items if i["id"] == item_id), None)
        if item:
            item.update(fields)
            store.write(FILE, items)
        return item


def decide(item_id: str, approved: bool) -> dict | None:
    """Approve a full video waiting for it (it is queued for upload) or skip it; None when it is not waiting."""
    with store.LOCK:
        item = get(item_id)
        if item is None or item["status"] != "waiting_approval":
            return None
        return update(item_id, **({"status": "queued", "approved_at": now_iso()} if approved else {"status": "skipped"}))


def retry(item_id: str) -> dict | None:
    """Put a failed item back in the queue; None when it did not fail."""
    with store.LOCK:
        item = get(item_id)
        if item is None or item["status"] != "failed":
            return None
        return update(item_id, status="queued", attempts=0, error=None, next_attempt_at=None)


def remove_where(keep) -> None:
    """Drop every item for which `keep(item)` is false."""
    with store.LOCK:
        store.write(FILE, [i for i in all_items() if keep(i)])


def drop_unsent_shorts(project: str) -> int:
    """Remove a project's shorts that have not gone out (scheduled, waiting for a retry or failed);
    returns how many. Used when the project switches its shorts uploads off."""
    gone = ("scheduled", "queued", "failed")
    with store.LOCK:
        items = all_items()
        kept = [i for i in items if not (i["kind"] == SHORT and i["project"] == project and i["status"] in gone)]
        if len(kept) != len(items):
            store.write(FILE, kept)
        return len(items) - len(kept)


def set_progress(item_id: str, fraction: float) -> None:
    _progress[item_id] = fraction


def progress_of(item_id: str) -> float | None:
    return _progress.get(item_id)


def url_of(item: dict) -> str | None:
    return f"https://youtu.be/{item['video_id']}" if item.get("video_id") else None


def public(item: dict) -> dict:
    hidden = {"file", "thumbnail", "session_url"}
    return {**{k: v for k, v in item.items() if k not in hidden}, "has_thumbnail": bool(item.get("thumbnail")),
            "url": url_of(item), "progress": progress_of(item["id"]) if item["status"] == "uploading" else None}

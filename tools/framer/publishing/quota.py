"""The YouTube Data API's daily quota. It belongs to the Google Cloud project (the OAuth client), not
to a channel, so usage is counted per client id and starts over at midnight Pacific time."""
from __future__ import annotations

from publishing import pacific, store

FILE = "quota.json"
DEFAULT_LIMIT = 10_000
LOW_FRACTION = 0.8


def _state() -> dict:
    return store.read(FILE, {})


def limit() -> int:
    return int(_state().get("limit") or DEFAULT_LIMIT)


def set_limit(units: int) -> None:
    with store.LOCK:
        store.write(FILE, {**_state(), "limit": max(1, int(units))})


def used(client_id: str) -> int:
    entry = _state().get("usage", {}).get(client_id) or {}
    return entry.get("used", 0) if entry.get("day") == pacific.today() else 0


def remaining(client_id: str) -> int:
    return max(0, limit() - used(client_id))


def spend(client_id: str, units: int) -> None:
    with store.LOCK:
        state = _state()
        state.setdefault("usage", {})[client_id] = {"day": pacific.today(), "used": used(client_id) + units}
        store.write(FILE, state)


def exhaust(client_id: str) -> None:
    spend(client_id, remaining(client_id))


def is_low(client_id: str) -> bool:
    return used(client_id) >= limit() * LOW_FRACTION


def snapshot(client_id: str | None) -> dict:
    return {"limit": limit(), "used": used(client_id) if client_id else 0,
            "resets_in_s": round(pacific.seconds_until_reset())}

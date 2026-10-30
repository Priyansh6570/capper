"""The connected YouTube channels. One file per channel under publishing/channels/, holding its
OAuth client, its token and its own settings (the shorts schedule, the playlist of each series)."""
from __future__ import annotations

from publishing import store

DEFAULT_SHORTS = {"per_day": 3, "from_hour": 9, "to_hour": 21}


def _file(channel_id: str) -> str:
    return f"channels/{channel_id}.json"


def get(channel_id: str | None) -> dict | None:
    return store.read(_file(channel_id), None) if channel_id else None


def all_channels() -> list[dict]:
    folder = store.path("channels")
    names = sorted(p.stem for p in folder.glob("*.json")) if folder.is_dir() else []
    return [c for c in map(get, names) if c]


def save(channel: dict) -> None:
    with store.LOCK:
        store.write(_file(channel["id"]), channel)


def update(channel_id: str, **fields) -> dict | None:
    with store.LOCK:
        channel = get(channel_id)
        if channel:
            channel.update(fields)
            save(channel)
        return channel


def delete(channel_id: str) -> None:
    store.path(_file(channel_id)).unlink(missing_ok=True)


def new(info: dict, client: dict, token: dict) -> dict:
    """The record of a freshly connected channel; reconnecting keeps the settings it already had."""
    previous = get(info["id"]) or {}
    return {"shorts": DEFAULT_SHORTS, "playlists": {}, "public_locked": False, **previous,
            "id": info["id"], "title": info["title"], "avatar": info["avatar"],
            "client": client, "token": token, "needs_reconnect": False}


def public(channel: dict) -> dict:
    """What the browser may see: never the token or the client secret."""
    return {k: channel[k] for k in ("id", "title", "avatar", "shorts", "public_locked", "needs_reconnect")}

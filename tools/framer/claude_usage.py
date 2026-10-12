"""Subscription usage as Claude Code reports it.

Every `claude -p` call streams a `rate_limit_event` whose `rate_limit_info.unifiedWindows`
holds `five_hour` and `seven_day` entries, each `{utilization, resetsAt}`: `utilization` is a
0-1 fraction of the window used and `resetsAt` is epoch seconds. (The status-line
`rate_limits.*.used_percentage` fields are not available to `-p` calls: the status line
only runs in the interactive UI.) The newest value seen is kept in `claude_usage.json`, so
it survives a restart and any process can read it between calls; `probe()` refreshes it
with a one-word call when it is stale.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time

from atomic_io import atomic_write_json, read_json_with_backup_fallback
from config import NO_CONSOLE, ROOT

WINDOWS = ("five_hour", "seven_day")
STALE_AFTER_S = 15 * 60
PATH = ROOT / "claude_usage.json"

_LOCK = threading.Lock()


def record(rate_limit_info: dict) -> None:
    """Store the windows of one `rate_limit_event`'s `rate_limit_info`."""
    seen = rate_limit_info.get("unifiedWindows") or {}
    with _LOCK:
        snapshot = _read()
        for window in WINDOWS:
            if window in seen:
                snapshot[window] = {"percent": round(seen[window]["utilization"] * 100, 1),
                                    "resets_at": seen[window]["resetsAt"]}
        snapshot["seen_at"] = time.time()
        atomic_write_json(PATH, snapshot, backup=False)


def _read() -> dict:
    try:
        return read_json_with_backup_fallback(PATH)
    except (OSError, ValueError):
        return {}


def snapshot() -> dict:
    """{five_hour: {percent, resets_at}, seven_day: {...}, seen_at} as last seen; windows
    whose reset has passed are dropped, since their percentage no longer applies."""
    with _LOCK:
        data = _read()
    now = time.time()
    return {**{w: data[w] for w in WINDOWS if w in data and data[w]["resets_at"] > now},
            "seen_at": data.get("seen_at")}


def is_current(data: dict) -> bool:
    return (all(w in data for w in WINDOWS) and data["seen_at"] is not None
            and time.time() - data["seen_at"] < STALE_AFTER_S)


def probe() -> dict:
    """Make the cheapest possible call so the CLI reports current usage, and return the snapshot."""
    proc = subprocess.run(
        [shutil.which("claude"), "-p", "--output-format", "stream-json", "--verbose", "--tools", "",
         "--model", "haiku", "--no-session-persistence"],
        input="Reply with the single word OK.", capture_output=True, text=True, encoding="utf-8",
        timeout=120, cwd=tempfile.gettempdir(), **NO_CONSOLE)
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "rate_limit_event":
            record(event["rate_limit_info"])
    return snapshot()


def current() -> dict:
    """The snapshot, refreshed by a probe when it is stale or a window has reset."""
    data = snapshot()
    return data if is_current(data) else probe()

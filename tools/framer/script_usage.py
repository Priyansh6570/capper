"""A line per finished script run with its tokens and quality mode, so the modes can be compared on real chapters."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

FIELDS = ("input", "output", "cache_read", "cache_write", "cost_usd", "seconds")


def record(path: Path, *, project: str, chapter: str, mode: str, panels: int, usage: dict) -> None:
    line = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "project": project, "chapter": chapter,
            "mode": mode, "panels": panels, **{field: usage[field] for field in FIELDS}}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(line) + "\n")

"""Where publishing keeps its state: on this PC only, outside every project folder, so nothing
here (tokens, bot token, queue) is synced to Drive or committed."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from atomic_io import atomic_write_json, read_json_with_backup_fallback
from config import ROOT

PUBLISH_DIR = ROOT / "publishing"
LOCK = threading.RLock()   # guards every read-modify-write of the files below


def path(*parts: str) -> Path:
    return PUBLISH_DIR.joinpath(*parts)


def read(name: str, default: Any) -> Any:
    try:
        return read_json_with_backup_fallback(path(name))
    except (OSError, ValueError):
        return default


def write(name: str, data: Any) -> None:
    atomic_write_json(path(name), data)

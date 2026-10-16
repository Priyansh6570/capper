"""App-wide settings that belong to this PC rather than to a project, kept in app_settings.json."""
from __future__ import annotations

import json
from pathlib import Path

from atomic_io import atomic_write_text
from config import ROOT

SETTINGS_FILE = ROOT / "app_settings.json"


def _read() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def projects_folder() -> Path | None:
    """Where new projects are created and shared projects are found, or None for the app's own projects/ folder."""
    folder = _read().get("projects_folder")
    return Path(folder) if folder else None


def set_projects_folder(folder: str) -> Path | None:
    path = Path(folder.strip()).expanduser() if folder.strip() else None
    if path:
        path.mkdir(parents=True, exist_ok=True)
    settings = {**_read(), "projects_folder": str(path) if path else None}
    atomic_write_text(SETTINGS_FILE, json.dumps(settings, indent=2), backup=False)
    return path

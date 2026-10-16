"""App-wide settings that belong to this PC rather than to a project, kept in app_settings.json."""
from __future__ import annotations

import json
from pathlib import Path

from atomic_io import atomic_write_text
from config import APP_SETTINGS_FILE

SETTINGS_FILE = APP_SETTINGS_FILE


def _read() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _folder(key: str) -> Path | None:
    folder = _read().get(key)
    return Path(folder) if folder else None


def _set_folder(key: str, folder: str) -> Path | None:
    path = Path(folder.strip()).expanduser() if folder.strip() else None
    if path:
        path.mkdir(parents=True, exist_ok=True)
    atomic_write_text(SETTINGS_FILE, json.dumps({**_read(), key: str(path) if path else None}, indent=2), backup=False)
    return path


def projects_folder() -> Path | None:
    """Where new projects are created and shared projects are found, or None for the app's own projects/ folder."""
    return _folder("projects_folder")


def set_projects_folder(folder: str) -> Path | None:
    return _set_folder("projects_folder", folder)


def model_cache() -> Path | None:
    """The Hugging Face cache folder the voice model is stored in (applied as HF_HOME, see config.py), or
    None for the Windows default under the user profile."""
    return _folder("hf_home")


def set_model_cache(folder: str) -> Path | None:
    return _set_folder("hf_home", folder)

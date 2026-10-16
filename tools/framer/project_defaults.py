"""The settings every new project starts with, kept in the repo so they ship with updates.

`defaults/settings.json` holds the settings and `defaults/assets/` the custom files they use
(voice sample, music, watermark logo, background, intro/outro). Both live in the app folder and are
tracked by git. API keys are never project settings, so they cannot end up here; the series brief
is specific to one series and is left out as well.

No Flask in here: app.py wires it to routes.
"""
from __future__ import annotations

import filecmp
import json
import shutil
from pathlib import Path

import projects
from atomic_io import atomic_write_text
from config import ROOT

DEFAULTS_DIR = ROOT / "defaults"
SETTINGS_FILE = DEFAULTS_DIR / "settings.json"
ASSETS_DIR = DEFAULTS_DIR / "assets"

NOT_SHARED = (("claude", "series_brief"),)
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
OPTION_PREFIX = "bg_"
ASSET_FIELDS = {
    "music": ("music",), "voice": ("voice",), "watermark": ("watermark",), "background": ("background",),
    "intro": ("branding", "intro"), "outro": ("branding", "outro"),
}
GROUP_LABELS = {
    "music": "Background music", "voice": "Narration voice", "watermark": "Watermark",
    "background": "Video background", "animation": "Frame animation", "video": "Video size",
    "branding": "Intro / outro", "gemini": "Script tone and style", "script_engine": "Script engine",
    "claude": "Claude script settings", "batch": "Batch automation", "framing": "Frame text blur",
}


def _lookup(settings: dict, path: tuple[str, ...]):
    for key in path:
        settings = settings[key]
    return settings


def _flatten(settings: dict, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], object]:
    flat = {}
    for key, value in settings.items():
        if isinstance(value, dict):
            flat.update(_flatten(value, prefix + (key,)))
        else:
            flat[prefix + (key,)] = value
    return flat


def _shared_part(settings: dict, schema: dict) -> dict:
    """`settings` cut down to the keys the app defines, without the per-series ones."""
    kept = {}
    for key, value in schema.items():
        if isinstance(value, dict):
            kept[key] = _shared_part(settings.get(key) or {}, value)
        elif key in settings:
            kept[key] = settings[key]
    for group, key in NOT_SHARED:
        kept.get(group, {}).pop(key, None)
    return kept


def load() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _default_asset(rel: str | None) -> Path | None:
    path = ASSETS_DIR / Path(rel or "").name
    return path if rel and path.is_file() else None


def save_from(slug: str) -> dict:
    """Make `slug`'s settings, and the custom files they point at, the defaults."""
    settings = _shared_part(projects.load(slug)["settings"], projects.default_settings())
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    for kind in ASSET_FIELDS:
        for old in ASSETS_DIR.glob(f"{kind}.*"):
            old.unlink()
    for path in ASSET_FIELDS.values():
        field = _lookup(settings, path)
        source = projects.resolve_asset_path(slug, field.get("file"))
        if source is None:
            field["file"] = None
        else:
            shutil.copy2(source, ASSETS_DIR / source.name)
            field["file"] = f"assets/{source.name}"
    atomic_write_text(SETTINGS_FILE, json.dumps(settings, indent=2, ensure_ascii=False), backup=False)
    return settings


def _same_file(slug: str, rel: str | None, default_rel: str | None) -> bool:
    mine, shipped = projects.resolve_asset_path(slug, rel), _default_asset(default_rel)
    if mine is None or shipped is None:
        return mine is shipped
    return filecmp.cmp(mine, shipped, shallow=False)


def changes(slug: str) -> list[dict]:
    """What applying the defaults to `slug` would change: [{group, name, current, default}]."""
    wanted = load()
    current = projects.load(slug)["settings"]
    found = []
    for path, default in _flatten(wanted).items():
        value = _lookup(current, path[:-1]).get(path[-1])
        if path[-1] == "file":
            if _same_file(slug, value, default):
                continue
            value, default = (Path(v).name if v else "none" for v in (value, default))
            default += " (shipped file)" if default != "none" else ""
        elif value == default:
            continue
        found.append({"group": GROUP_LABELS[path[0]], "name": " ".join(path[1:]).replace("_", " ") or path[0].replace("_", " "),
                      "current": value, "default": default})
    return found


def apply(slug: str) -> None:
    """Make `slug` use the defaults. Custom files the project already has are never deleted."""
    wanted = load()
    if not wanted:
        return
    for kind, path in ASSET_FIELDS.items():
        shipped = _default_asset(_lookup(wanted, path).get("file"))
        if shipped:
            for old in projects.assets_dir(slug).glob(f"{kind}.*"):
                old.unlink(missing_ok=True)
            shutil.copy2(shipped, projects.assets_dir(slug) / shipped.name)
    projects.update_settings(slug, wanted)


def background_options() -> list[dict]:
    """The ready-made background videos in `defaults/assets/`: `background.<ext>` (the default) and
    every `bg_<name>.<ext>`."""
    files = sorted(p for p in ASSETS_DIR.glob("*") if p.suffix.lower() in VIDEO_EXTS
                   and (p.stem == "background" or p.stem.startswith(OPTION_PREFIX)))
    return [{"id": p.name, "label": "Default" if p.stem == "background"
             else p.stem[len(OPTION_PREFIX):].replace("-", " ").replace("_", " ").capitalize()} for p in files]


def background_option(option_id: str) -> Path | None:
    return next((ASSETS_DIR / o["id"] for o in background_options() if o["id"] == option_id), None)


def current_background_option(slug: str) -> str | None:
    mine = projects.resolve_asset_path(slug, projects.load(slug)["settings"]["background"].get("file"))
    return next((o["id"] for o in background_options()
                 if mine and filecmp.cmp(mine, ASSETS_DIR / o["id"], shallow=False)), None)


def select_background(slug: str, option_id: str) -> None:
    """Use a ready-made video as the project's background (replacing its own background file)."""
    source = background_option(option_id)
    if source is None:
        raise ValueError(f"No such background video: {option_id}")
    name = f"background{source.suffix.lower()}"
    for old in projects.assets_dir(slug).glob("background.*"):
        old.unlink(missing_ok=True)
    shutil.copy2(source, projects.assets_dir(slug) / name)
    projects.update_settings(slug, {"background": {"mode": "video", "file": f"assets/{name}"}})


def create_project(name: str, url: str) -> dict:
    project = projects.create(name, url)
    apply(project["slug"])
    return projects.load(project["slug"])

"""A project's music library for its shorts: any number of tracks, each marked with a theme. Stage 8 picks
the track of the theme Claude chose for a short and renders the short as long as that track (see
stages/s8_shorts.py). The tracks live in projects/<slug>/assets/shorts_music/, their list in
`settings.shorts.library`, and reach the stage through the media snapshot (`shorts.library`)."""
from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

import projects
from branding import ffprobe_path
from config import NO_CONSOLE

THEMES = ("comedic", "aura", "emotional", "serious", "dramatic", "op", "action", "mystery", "sad", "hype")
FOLDER = "shorts_music"
EXTS = projects._ASSET_EXTS["music"]
MAX_THEME = 24


class LibraryError(ValueError):
    pass


def clean_theme(theme) -> str:
    """A theme as a lowercase word or two (letters, digits, dashes); one of THEMES or a custom one."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(theme or "").lower()).strip("-")[:MAX_THEME]
    if not slug:
        raise LibraryError("Give the music a theme.")
    return slug


def clean_drop(drop, duration: float) -> float | None:
    """The drop point in seconds (None/blank: the track has none); it has to fall inside the track."""
    if drop in (None, ""):
        return None
    try:
        seconds = round(float(drop), 1)
    except (TypeError, ValueError):
        raise LibraryError("The drop point is a number of seconds.") from None
    if not 0 < seconds < duration:
        raise LibraryError(f"The drop point must be between 0 and {duration:.0f} seconds.")
    return seconds


def duration_of(path: Path) -> float:
    try:
        out = subprocess.run([ffprobe_path(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=30, **NO_CONSOLE)
        return float(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def library(project: dict) -> list[dict]:
    return project["settings"]["shorts"]["library"]


def themes_of(project: dict) -> list[str]:
    """The distinct themes the project has music for, in the order first added."""
    return list(dict.fromkeys(t["theme"] for t in library(project)))


def add(slug: str, filename: str, file_storage, theme: str, drop=None) -> dict:
    theme = clean_theme(theme)
    ext = Path(filename or "").suffix.lower()
    if ext not in EXTS:
        raise LibraryError(f"Unsupported music file: {ext or '(none)'}")
    track_id = uuid.uuid4().hex[:8]
    folder = projects.assets_dir(slug) / FOLDER
    folder.mkdir(exist_ok=True)
    dest = folder / f"{track_id}{ext}"
    file_storage.save(str(dest))
    seconds = duration_of(dest)
    if seconds <= 0:
        dest.unlink(missing_ok=True)
        raise LibraryError("That file could not be read as audio.")
    try:
        drop_s = clean_drop(drop, seconds)
    except LibraryError:
        dest.unlink(missing_ok=True)
        raise
    track = {"id": track_id, "name": Path(filename).stem[:80], "theme": theme, "duration_s": round(seconds, 1),
             "drop_s": drop_s, "file": f"assets/{FOLDER}/{dest.name}"}
    with projects._LOCK:
        project = projects.load(slug)
        library(project).append(track)
        projects.save(project)
    projects.write_all_media_snapshots(slug)
    return track


def _find(project: dict, track_id: str) -> dict:
    track = next((t for t in library(project) if t["id"] == track_id), None)
    if track is None:
        raise LibraryError("No such track.")
    return track


def update(slug: str, track_id: str, theme: str | None = None, name: str | None = None, **fields) -> dict:
    """Change a track's theme, name or (with `drop_s=`, None clears it) drop point."""
    with projects._LOCK:
        project = projects.load(slug)
        track = _find(project, track_id)
        if theme is not None:
            track["theme"] = clean_theme(theme)
        if "drop_s" in fields:
            track["drop_s"] = clean_drop(fields["drop_s"], track["duration_s"])
        if name is not None and name.strip():
            track["name"] = name.strip()[:80]
        projects.save(project)
    projects.write_all_media_snapshots(slug)
    return track


def remove(slug: str, track_id: str) -> None:
    with projects._LOCK:
        project = projects.load(slug)
        track = _find(project, track_id)
        project["settings"]["shorts"]["library"] = [t for t in library(project) if t["id"] != track_id]
        projects.save(project)
    path = projects.resolve_asset_path(slug, track["file"])
    if path:
        path.unlink(missing_ok=True)
    projects.write_all_media_snapshots(slug)

"""Backing a chapter's working files up to the project's shared (Drive) folder, and getting them back.

A project kept in the shared folder only has `chapter.json` and the chapter PDF there; the script,
key mappings, frames, narration and rendered video live on this PC (`projects/<slug>/chapters/<n>/
{work,output}`). This module mirrors those files into the project's folder in the shared drive,
using the same relative layout, so Drive for desktop streams each file back when it is read.

Per chapter the files fall into kinds (KINDS). `clearable` kinds are the heavy ones: once they are
verified in the backup they can be deleted from this PC, and are copied back the next time the
chapter is used (see `ensure_local`, called from `projects.chapter_work_dir/chapter_output_dir`).
The script and mappings are small and always stay on this PC.

A backup is verified by size: every local file of the kind has a file of the same size in the
drive copy. No Flask in here.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import projects
from atomic_io import atomic_write_text

MANIFEST = "backup.json"      # in the drive copy of the chapter: what was backed up and when
CLEARED = ".cleared.json"     # in the local work folder: the kinds whose files were removed from this PC


@dataclass(frozen=True)
class Kind:
    key: str
    label: str
    hint: str
    clearable: bool
    paths: tuple[str, ...]    # relative to the chapter folder; a folder means everything under it


KINDS: dict[str, Kind] = {k.key: k for k in (
    Kind("script", "Script and key mappings", "Narration script, frame boxes and chapter settings. Small.", False,
         ("work/claude", "work/framer/mapping.json", "work/framer/preview.json", "work/manifest.json",
          "work/manifest.json.bak", "work/media_settings.json", "work/media_settings.json.bak")),
    Kind("frames", "Frames", "The cropped frames shown in the video.", True, ("work/frames",)),
    Kind("audio", "Narration audio", "The voiced lines.", True, ("work/audio",)),
    Kind("pages", "Page images", "The stitched strip, panels and editor tiles. Can be rebuilt from the PDF.", True,
         ("work/pages", "work/panels", "work/framer/tiles")),
    Kind("video", "Chapter video", "The rendered recap.mp4.", True, ("output",)),
)}

_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class BackupUnavailable(RuntimeError):
    """The shared folder can't be reached, so a cleared chapter can't be restored right now."""


def is_shared(slug: str) -> bool:
    """True when the project's folder is somewhere other than this PC's own projects/ folder."""
    return projects.project_dir(slug) != projects.local_dir(slug)


def _local_root(slug: str, n) -> Path:
    return projects.chapter_dir(slug, n)


def _drive_root(slug: str, n) -> Path:
    return projects.stored_chapter_dir(slug, n)


def _files(root: Path, kind: Kind) -> list[Path]:
    """The kind's files under `root`, as paths relative to it."""
    found: list[Path] = []
    for rel in kind.paths:
        target = root / rel
        if target.is_file():
            found.append(Path(rel))
        elif target.is_dir():
            found.extend(p.relative_to(root) for p in sorted(target.rglob("*"))
                         if p.is_file() and not p.name.endswith(".part"))
    return found


def _size(path: Path) -> Optional[int]:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _same(local: Path, remote: Path) -> bool:
    size = _size(local)
    return size is not None and size == _size(remote)


# --------------------------------------------------------------------------- #
# the cleared marker
# --------------------------------------------------------------------------- #
def _marker(slug: str, n) -> Path:
    return _local_root(slug, n) / "work" / CLEARED


def cleared_kinds(slug: str, n) -> list[str]:
    try:
        return [k for k in json.loads(_marker(slug, n).read_text(encoding="utf-8")) if k in KINDS]
    except (OSError, ValueError):
        return []


def _write_cleared(slug: str, n, kinds: Iterable[str]) -> None:
    path, kinds = _marker(slug, n), sorted(set(kinds))
    if kinds:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(kinds), backup=False)
    else:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def kind_status(slug: str, n, kind: Kind) -> dict:
    """{local_bytes, files, backed_up, cleared}. `backed_up` means: there are local files and every one
    has a same-size copy in the drive folder."""
    local, drive = _local_root(slug, n), _drive_root(slug, n)
    files = _files(local, kind)
    total = sum(_size(local / rel) or 0 for rel in files)
    cleared = kind.key in cleared_kinds(slug, n)
    backed_up = bool(files) and all(_same(local / rel, drive / rel) for rel in files)
    return {"local_bytes": total, "files": len(files), "backed_up": backed_up or (cleared and not files),
            "cleared": cleared}


def chapter_status(slug: str, n) -> dict:
    return {key: kind_status(slug, n, kind) for key, kind in KINDS.items()}


def overview(slug: str, numbers: Iterable) -> list[dict]:
    """One entry per chapter that has anything on this PC (or in the drive copy)."""
    out = []
    for n in numbers:
        status = chapter_status(slug, n)
        if any(s["files"] or s["cleared"] for s in status.values()):
            out.append({"n": n, "kinds": status})
    return out


# --------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------- #
def _copy(src: Path, dest: Path) -> None:
    """Copy through a .part file, renamed into place only when complete, so Drive never syncs half a file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    shutil.copy2(src, partial)
    os.replace(partial, dest)


def backup_kind(slug: str, n, kind_key: str, progress: Callable[[int, int], None] = lambda done, total: None,
                stop: Optional[threading.Event] = None) -> dict:
    """Copy the kind's files that are missing or different in the drive folder. Returns {copied, bytes}."""
    kind = KINDS[kind_key]
    local, drive = _local_root(slug, n), _drive_root(slug, n)
    files = _files(local, kind)
    copied = copied_bytes = 0
    for i, rel in enumerate(files, 1):
        if stop is not None and stop.is_set():
            break
        if not _same(local / rel, drive / rel):
            _copy(local / rel, drive / rel)
            copied += 1
            copied_bytes += _size(local / rel) or 0
        progress(i, len(files))
    _record(slug, n, kind_key, files)
    return {"copied": copied, "bytes": copied_bytes}


def _record(slug: str, n, kind_key: str, files: list[Path]) -> None:
    path = _drive_root(slug, n) / MANIFEST
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[kind_key] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "files": len(files)}
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(data, indent=2), backup=False)


# --------------------------------------------------------------------------- #
# clear + restore
# --------------------------------------------------------------------------- #
def clear_kind(slug: str, n, kind_key: str) -> int:
    """Delete the kind's files from this PC, but only when every one is verified in the drive copy.
    Returns the bytes freed; raises ValueError when the kind isn't clearable or isn't safely backed up."""
    kind = KINDS[kind_key]
    if not kind.clearable:
        raise ValueError(f"{kind.label} stays on this PC.")
    status = kind_status(slug, n, kind)
    if status["cleared"] or not status["files"]:
        return 0
    if not status["backed_up"]:
        raise ValueError(f"{kind.label} isn't fully backed up yet.")
    local = _local_root(slug, n)
    freed = 0
    for rel in _files(local, kind):
        freed += _size(local / rel) or 0
        (local / rel).unlink()
    for rel in kind.paths:                                  # drop the now-empty folders
        target = local / rel
        if target.is_dir():
            for folder in sorted((p for p in target.rglob("*") if p.is_dir()), reverse=True):
                try:
                    folder.rmdir()
                except OSError:
                    pass
            try:
                target.rmdir()
            except OSError:
                pass
    _write_cleared(slug, n, [*cleared_kinds(slug, n), kind_key])
    return freed


def _lock(slug: str, n) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault((slug, str(n)), threading.Lock())


def restore(slug: str, n, kinds: Optional[Iterable[str]] = None,
            progress: Callable[[int, int], None] = lambda done, total: None) -> int:
    """Copy the cleared kinds (or just `kinds`) back from the drive copy. Returns the bytes restored.
    Raises BackupUnavailable when the drive copy can't be read."""
    with _lock(slug, n):
        wanted = [k for k in (kinds or cleared_kinds(slug, n)) if k in cleared_kinds(slug, n)]
        if not wanted:
            return 0
        local, drive = _local_root(slug, n), _drive_root(slug, n)
        jobs = [(kind, rel) for kind in wanted for rel in _files(drive, KINDS[kind])]
        if not jobs and not drive.exists():
            raise BackupUnavailable(f"Chapter {n} was cleared from this PC and backed up to {drive}, "
                                    "but that folder can't be reached. Connect the drive and try again.")
        restored = 0
        try:
            for i, (kind, rel) in enumerate(jobs, 1):
                if not _same(drive / rel, local / rel):
                    _copy(drive / rel, local / rel)
                restored += _size(local / rel) or 0
                progress(i, len(jobs))
        except OSError as e:
            raise BackupUnavailable(f"Couldn't read chapter {n} back from {drive}: {e.strerror or e}") from e
        _write_cleared(slug, n, [k for k in cleared_kinds(slug, n) if k not in wanted])
        return restored


def ensure_local(slug: str, n) -> None:
    """Called whenever a chapter's folders are about to be used: bring back anything that was cleared."""
    if cleared_kinds(slug, n):
        restore(slug, n)

"""Crash-safe file writes, shared by every place a user's boxing/script work
lives on disk (the framer's editor state + autosave, the exported line/frame
mapping, the pipeline manifest, project.json, media settings, ...).

Not a database - just two guarantees layered on plain files:

1. ATOMIC: new content is written to a temp file IN THE SAME DIRECTORY,
   flushed + fsync'd to actually hit disk, then swapped into place with
   `os.replace()` - a single directory-entry update on both NTFS and POSIX
   filesystems. A crash, kill, or power loss at any point during a save
   leaves either the OLD file completely intact or the NEW one completely
   intact - never a half-written, truncated, or empty file.

2. BACKED UP: right before that swap, whatever was PREVIOUSLY at the target
   path is preserved at `<path>.bak` (written the same crash-safe way, and
   never overwritten with empty/corrupt content). So even a save that
   completes but writes wrong data - an app bug, not a crash - can be rolled
   back to the one before it. `read_json_with_backup_fallback` is the read
   side of that: if the primary file is ever missing/empty/unparsable, it
   transparently falls back to `.bak` instead of raising.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

# Per-path locks so two threads in the same process (the Flask app runs
# threaded=True) never race on the SAME file's save. This matters even though
# each individual os.replace() is atomic: on Windows, os.replace() onto a
# destination that another thread is simultaneously replacing (or that a
# second thread's own backup step is simultaneously reading/renaming) can
# transiently raise PermissionError ("Access is denied") instead of just
# serializing - confirmed empirically with a concurrent-writers stress test,
# not theoretical. A per-path lock makes every save-to-that-path fully
# serialized, so this can't happen; it does not protect against a second
# writer PROCESS (this app never runs more than one against the same chapter,
# per _resolve_port's single-instance guard).
_locks_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}


def _lock_for(path: Path) -> threading.Lock:
    # normcase+abspath (not resolve()) so this never requires the path or its
    # parent to already exist on disk.
    key = os.path.normcase(os.path.abspath(str(path)))
    with _locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = _path_locks[key] = threading.Lock()
        return lock


def _write_temp(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # uuid (not just pid) so two threads in the same process saving the same
    # path concurrently never collide on one temp file.
    tmp = path.with_name(path.name + f".tmp{uuid.uuid4().hex[:8]}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    return tmp


def atomic_write_text(path: Path, text: str, *, backup: bool = True) -> None:
    """Crash-safe write of `text` to `path`. See module docstring."""
    path = Path(path)
    with _lock_for(path):
        if backup and path.exists():
            try:
                old_text = path.read_text(encoding="utf-8")
                if old_text.strip():  # don't let an already-corrupt file clobber a good .bak
                    bak_path = path.with_name(path.name + ".bak")
                    os.replace(_write_temp(bak_path, old_text), bak_path)
            except OSError:
                pass  # a failed backup must never block the real save
        os.replace(_write_temp(path, text), path)


def atomic_write_json(path: Path, obj: Any, *, backup: bool = True, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(obj, indent=indent, ensure_ascii=False), backup=backup)


def read_json_with_backup_fallback(path: Path) -> Any:
    """Load JSON from `path`; if it's missing, empty, or fails to parse (the
    corruption atomic writes are meant to prevent - this is the belt to that
    suspenders), transparently fall back to `<path>.bak`."""
    path = Path(path)
    last_err: Exception | None = None
    for candidate in (path, path.with_name(path.name + ".bak")):
        if not candidate.exists():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
            if not text.strip():
                raise ValueError("empty file")
            return json.loads(text)
        except (OSError, ValueError) as e:
            last_err = e
            continue
    raise FileNotFoundError(f"No readable JSON at {path} or its .bak") from last_err

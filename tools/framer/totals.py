"""Project totals that come from every chapter's files - no Flask here.

The speaking pace of the generated narration and the Claude tokens spent are sums over chapters.
Each chapter's share is cached on this PC (totals.json in the local project folder) together with
a fingerprint of the files it was read from, so a chapter whose manifest, audio or Claude state
changed is read again on the next call and the totals never go stale. Chapters nobody has worked
on have no local folder and are not looked at.
"""
from __future__ import annotations

import json
from pathlib import Path

import claude_cli
import claude_engine
import projects
from atomic_io import atomic_write_text
from manifest import Manifest

MIN_RATE_CHARS = 20           # shorter clips are mostly pauses and say little about speaking rate
CACHE_FILE = "totals.json"


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _fingerprint(work: Path) -> list[int]:
    """Changes whenever the beats, the audio files (the folder changes when one is added or deleted)
    or the Claude usage a chapter's share is read from change."""
    return [_mtime(work / "manifest.json"), _mtime(work / "audio"), _mtime(work / "claude" / "state.json")]


def _chapter_share(work: Path) -> dict:
    chars = seconds = 0.0
    try:
        beats = Manifest.load(work).beats
    except (OSError, ValueError, KeyError):
        beats = []
    for beat in beats:
        if beat.duration_s and beat.audio and Path(beat.audio).is_file() and len(beat.script_line or "") >= MIN_RATE_CHARS:
            chars += len(beat.script_line)
            seconds += beat.duration_s
    usage = claude_engine.read_usage(work / "claude" / "state.json") or {}
    return {"speech": [chars, seconds], "claude": usage.get("total")}


def _worked_chapters(slug: str) -> list[int]:
    chapters = projects.local_dir(slug) / "chapters"
    return sorted(int(d.name) for d in chapters.iterdir() if d.name.isdigit() and (d / "work").is_dir()) \
        if chapters.is_dir() else []


def _shares(slug: str) -> dict[str, dict]:
    cache_path = projects.local_dir(slug) / CACHE_FILE
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8")).get("chapters", {})
    except (OSError, ValueError):
        cached = {}
    current = {}
    for n in _worked_chapters(slug):
        work = projects.chapter_work_path(slug, n)
        fingerprint = _fingerprint(work)
        entry = cached.get(str(n))
        current[str(n)] = entry if entry and entry["fingerprint"] == fingerprint \
            else {"fingerprint": fingerprint, **_chapter_share(work)}
    if current != cached:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(cache_path, json.dumps({"chapters": current}), backup=False)
    return current


def speech_rate(slug: str) -> float | None:
    """Characters per second of the project's generated narration audio, else None."""
    shares = _shares(slug).values()
    seconds = sum(share["speech"][1] for share in shares)
    return sum(share["speech"][0] for share in shares) / seconds if seconds else None


def claude_usage(slug: str) -> dict:
    """The Claude tokens spent on the project's chapters, summed."""
    return claude_cli.sum_usage([s["claude"] for s in _shares(slug).values() if s["claude"]])

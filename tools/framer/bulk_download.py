"""Download a run of chapters for a project into its project folder - no Flask here.

Chapters already stored in the project folder are skipped, so a stopped or failed run is resumed by
running it again.
"""
from __future__ import annotations

import time
from typing import Callable

import projects

PAUSE_BETWEEN_CHAPTERS_S = 5


def missing_chapters(slug: str) -> list[int]:
    return [ch["n"] for ch in projects.load(slug)["chapters"] if projects.stored_pdf(slug, ch["n"]) is None]


def run(slug: str, count: int, download_one: Callable[[int], None], progress: Callable[[int, int, int], None],
        should_stop: Callable[[], bool]) -> dict:
    """Download the next `count` missing chapters. `progress` gets (chapter, position, total)."""
    todo = missing_chapters(slug)[:count]
    failed = []
    for position, n in enumerate(todo, 1):
        if should_stop():
            break
        progress(n, position, len(todo))
        try:
            download_one(n)
        except Exception as e:  # noqa: BLE001 - one bad chapter must not stop the run
            failed.append({"chapter": n, "error": str(e)})
        if position < len(todo):
            time.sleep(PAUSE_BETWEEN_CHAPTERS_S)
    return {"requested": len(todo), "failed": failed}

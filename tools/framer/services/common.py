"""Helpers shared by the route and service modules: timestamps, event-stream frames,
request scoping and the error type routes raise."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from flask import request


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sse(obj: dict) -> str:
    """Format one Server-Sent Events 'message' frame. Shared by every SSE route
    (e.g. /api/projects/<slug>/merge_stream)."""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


# --------------------------------------------------------------------------- #
# project scoping - every route that touches chapter files redirects
# chapter_work_dir()/chapter_output_dir() (see config.py) to
# projects/<slug>/chapters/<n>/{work,output} for the duration of the request,
# by reading `project` + `ch_no` (query args, JSON body, or form field - never
# collides with the pre-existing `chapter`/`chapter_id` label param).
#
# NOTE on the SSE streams and the render job worker thread
# (_run_job, near /api/generate): Flask tears the request context down
# before a streamed generator's body actually runs, and the job worker isn't
# in a request context AT ALL (it's a persistent background thread, so a job
# outlives the request that enqueued it - see the note above JOBS). Neither
# can rely on before_request's scope, so both compute their own
# (work_dir, output_dir) up front and wrap their body in an explicit
# `with config.chapter_scope(...)`.
# --------------------------------------------------------------------------- #
def scope_params() -> tuple[str | None, str | None]:
    data = request.get_json(silent=True) if request.is_json else None
    data = data or {}
    form = request.form if request.form else {}
    slug = request.args.get("project") or data.get("project") or form.get("project")
    ch_no = request.args.get("ch_no") or data.get("ch_no") or form.get("ch_no")
    return (slug or None), (str(ch_no) if ch_no else None)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def is_under(path: Path, root: Path) -> bool:
    """True if `path` (already resolved) sits inside `root` (already resolved).
    normcase handles Windows' case-insensitive / drive-letter-cased paths."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return os.path.normcase(str(path)).startswith(os.path.normcase(str(root)))


def sanitize_chapter(name: str) -> str:
    """A safe chapter_id from arbitrary text (used as a folder name)."""
    name = (name or "").strip()
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return name or "ch_framer"


class Refused(Exception):
    """A request the app will not run; `status` is the HTTP status a route reports for it."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status

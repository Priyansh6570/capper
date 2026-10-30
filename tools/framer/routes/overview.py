"""Read-only overview of every project's pipelines: what is running, queued or waiting, and when it finishes.

Everything live comes from memory (the batches and the job registry), so the poll is cheap and never
opens a project. The project list (names, chapter counts) reads project.json files, so it is cached
and refreshed in the background; a request never waits for it once it has been built once."""
from __future__ import annotations

import threading
import time

from flask import Blueprint, jsonify, send_from_directory

import projects
from routes.web import HERE
from services.batching import BATCH_LOCK, BATCHES, SHORTS_BATCHES, job_summary
from services.jobs import JOBS, JOBS_LOCK, jobs_with_eta

bp = Blueprint("overview", __name__)

PROJECT_TTL_S = 15
FINISHED_TAIL = 8

_cache: dict = {"at": 0.0, "rows": None, "refreshing": False}
_cache_lock = threading.Lock()


def _refresh_projects() -> None:
    try:
        rows = {p["slug"]: p for p in projects.list_projects()}
    except Exception:  # noqa: BLE001 - a bad project folder must not break the overview
        rows = _cache["rows"] or {}
    with _cache_lock:
        _cache.update(at=time.time(), rows=rows, refreshing=False)


def _project_rows() -> dict:
    with _cache_lock:
        stale = time.time() - _cache["at"] > PROJECT_TTL_S
        first = _cache["rows"] is None
        if stale and not _cache["refreshing"] and not first:
            _cache["refreshing"] = True
            threading.Thread(target=_refresh_projects, daemon=True).start()
    if first:
        _refresh_projects()
    return _cache["rows"] or {}


def _batch_view(store: dict, slug: str, job_of) -> dict | None:
    current = store.get(slug)
    return current.summary(job_of) if current else None


@bp.get("/overview")
def overview_page():
    return send_from_directory(HERE / "static", "overview.html")


@bp.get("/api/overview")
def api_overview():
    now = time.time()
    etas = {j["id"]: j.get("eta_s") for j in jobs_with_eta()}

    def job_of(job_id: str) -> dict | None:
        job = job_summary(job_id)
        if job:
            job["eta_s"] = etas.get(job_id)
        return job

    rows = _project_rows()
    with BATCH_LOCK:
        slugs = set(BATCHES) | set(SHORTS_BATCHES)
    out, claimed = [], set()
    for slug in sorted(slugs | set(rows)):
        pipeline = _batch_view(BATCHES, slug, job_of)
        shorts = _batch_view(SHORTS_BATCHES, slug, job_of)
        for view in (pipeline, shorts):
            for ch in (view or {}).get("chapters", []):
                if ch["job"]:
                    claimed.add(ch["job"]["id"])
        row = rows.get(slug, {})
        out.append({"slug": slug, "name": row.get("name") or slug, "chapters": row.get("chapters", 0),
                    "counts": row.get("counts", {}), "cover": row.get("cover", 0),
                    "pipeline": pipeline, "shorts": shorts})

    with JOBS_LOCK:
        finished = sorted((j for j in JOBS.values() if j.status in ("done", "failed", "cancelled")),
                          key=lambda j: j.finished_at or "", reverse=True)[:FINISHED_TAIL]
        finished = [{k: v for k, v in j.public().items() if k != "log"} for j in finished]
    loose = [{k: v for k, v in j.items() if k != "log"} for j in jobs_with_eta() if j["id"] not in claimed]
    return jsonify(now=now, projects=out, loose_jobs=loose, finished=finished)

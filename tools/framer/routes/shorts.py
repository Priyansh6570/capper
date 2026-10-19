"""Routes for the Shorts pipeline: running it for chapters and listing what it made."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

import batch
import projects
import shorts
from services.batching import (BATCH_LOCK, SHORTS_BATCHES, batch_do, job_summary, shorts_batch_path, shorts_blocker,
                               shorts_env)

bp = Blueprint("shorts", __name__)


@bp.get("/api/projects/<slug>/shorts")
def api_shorts(slug):
    """Every chapter that has shorts (or a plan for them) with the shorts' titles, descriptions and
    files, and the project's Shorts batch (or null)."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    project = projects.load(slug)
    chapters = []
    for ch in project["chapters"]:
        made = shorts.rendered(projects.chapter_output_path(slug, ch["n"]))
        planned = shorts.read_plan(projects.chapter_work_path(slug, ch["n"]))
        if made or planned:
            chapters.append({"n": ch["n"], "title": ch["title"], "planned": len(planned), "shorts": made})
    current = SHORTS_BATCHES.get(slug)
    view = current.view(job_summary) if current else None
    for run in view["chapters"] if view else []:
        run["title"] = next((c["title"] for c in project["chapters"] if c["n"] == run["n"]), "")
        plan = shorts.read_plan(projects.chapter_work_path(slug, run["n"])) if "shorts_find" in run["completed"] else None
        run["planned"] = None if plan is None else [
            {k: s[k] for k in ("title", "first_line", "last_line", "duration_s")} for s in plan]
    return jsonify(chapters=chapters, batch=view)


@bp.get("/api/projects/<slug>/shorts/<int:n>/zip")
def api_shorts_zip(slug, n):
    """Every rendered short of chapter n, with their titles and descriptions, as one zip."""
    if not projects.exists(slug) or not shorts.rendered(projects.chapter_output_path(slug, n)):
        return jsonify(error="This chapter has no shorts."), 404
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        shorts.write_zip(projects.chapter_output_path(slug, n), Path(tmp))
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    response = send_file(tmp, as_attachment=True, download_name=f"{slug}-chapter-{n}-shorts.zip", mimetype="application/zip")
    response.call_on_close(lambda: Path(tmp).unlink(missing_ok=True))
    return response


@bp.post("/api/projects/<slug>/shorts/batch")
def api_shorts_start(slug):
    """Body: JSON {chapters: [n, ...]}. Makes shorts for those chapters that have a finished video
    (re-running a chapter replaces its shorts); the rest are reported as skipped. A Shorts batch that is
    already running takes them on as well."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    known = {c["n"] for c in projects.load(slug)["chapters"]}
    try:
        numbers = sorted({int(n) for n in (request.get_json(silent=True) or {}).get("chapters") or []})
    except (TypeError, ValueError):
        numbers = []
    if not numbers or not known.issuperset(numbers):
        return jsonify(error="Choose chapters of this project."), 400
    skipped = [{"n": n, "reason": reason} for n in numbers if (reason := shorts_blocker(slug, n))]
    eligible = [n for n in numbers if n not in {s["n"] for s in skipped}]
    if not eligible:
        return jsonify(error="None of these chapters can have shorts yet: " +
                             "; ".join(f"chapter {s['n']} {s['reason']}" for s in skipped) + "."), 409
    plans = {n: list(batch.SHORTS_STAGES) for n in eligible}
    with BATCH_LOCK:
        previous = SHORTS_BATCHES.get(slug)
        if previous and previous.active:
            try:
                previous.enqueue(plans)
            except ValueError as e:
                return jsonify(error=str(e)), 409
        else:
            if previous:
                previous.close()
            SHORTS_BATCHES[slug] = batch.Batch.create(shorts_batch_path(slug), shorts_env(slug), plans)
    return jsonify(ok=True, started=eligible, skipped=skipped)


@bp.post("/api/projects/<slug>/shorts/batch/<control>")
def api_shorts_control(slug, control):
    """control: pause | resume | cancel, for the whole Shorts batch."""
    if control not in ("pause", "resume", "cancel"):
        return jsonify(error="Unknown control."), 404
    return batch_do(slug, lambda b: getattr(b, control)(), SHORTS_BATCHES)

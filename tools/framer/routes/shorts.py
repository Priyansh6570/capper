"""Routes for the Shorts pipeline: running it for chapters and listing what it made."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

import projects
import shorts
from services.batching import SHORTS_BATCHES, batch_do, job_summary, start_shorts
from services.common import Refused

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
            chapters.append({"n": ch["n"], "title": ch["title"], "planned": len(planned), "shorts": made,
                             "plan": [{k: s.get(k) for k in ("id", "title", "description", "reason", "first_line",
                                                              "last_line", "duration_s", "score")} for s in planned]})
    current = SHORTS_BATCHES.get(slug)
    view = current.view(job_summary) if current else None
    for run in view["chapters"] if view else []:
        run["title"] = next((c["title"] for c in project["chapters"] if c["n"] == run["n"]), "")
        found = "shorts_find" in run["completed"] or "shorts_find" not in run["plan"]
        plan = shorts.read_plan(projects.chapter_work_path(slug, run["n"])) if found else None
        run["planned"] = None if plan is None else [
            {k: s[k] for k in ("title", "first_line", "last_line", "duration_s")} for s in plan]
    return jsonify(chapters=chapters, batch=view)


def _delete_guard(slug: str, n: int):
    """An error response when chapter n's shorts cannot be deleted now, else None."""
    if not projects.exists(slug) or n not in {c["n"] for c in projects.load(slug)["chapters"]}:
        return jsonify(error="No such chapter."), 404
    current = SHORTS_BATCHES.get(slug)
    if current and current.active:
        return jsonify(error="Shorts are being made for this project; stop them first."), 409
    return None


@bp.delete("/api/projects/<slug>/shorts/<int:n>")
def api_shorts_delete_chapter(slug, n):
    """Delete all of chapter n's shorts: the plan and the videos."""
    if (refused := _delete_guard(slug, n)):
        return refused
    shorts.clear(projects.chapter_work_path(slug, n), projects.chapter_output_path(slug, n))
    return jsonify(ok=True)


@bp.delete("/api/projects/<slug>/shorts/<int:n>/<int:short_id>")
def api_shorts_delete_one(slug, n, short_id):
    """Delete one short of chapter n: its video and its place in the plan."""
    if (refused := _delete_guard(slug, n)):
        return refused
    if not shorts.delete_short(projects.chapter_work_path(slug, n), projects.chapter_output_path(slug, n), short_id):
        return jsonify(error="No such short."), 404
    return jsonify(ok=True)


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
    """Body: JSON {chapters: [n, ...]} or {first, last} (a range), and optionally {max_shorts}. Claude studies
    those chapters that have a finished video as one range and the shorts it finds, in whichever chapters have a
    strong moment, are rendered; the other chapters get none, and any shorts they already have stay as they
    are. The rest of the range is reported as skipped. A chapter whose moments are picked again has its
    shorts replaced."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    known = {c["n"] for c in projects.load(slug)["chapters"]}
    body = request.get_json(silent=True) or {}
    try:
        numbers = ({n for n in known if int(body["first"]) <= n <= int(body["last"])} if "first" in body
                   else {int(n) for n in body.get("chapters") or []})
        max_shorts = int(body["max_shorts"]) if body.get("max_shorts") not in (None, "") else None
    except (TypeError, ValueError, KeyError):
        numbers, max_shorts = set(), None
    if not numbers or not known.issuperset(numbers) or (max_shorts is not None and max_shorts < 1):
        return jsonify(error="Choose chapters of this project, and a number of shorts of 1 or more."), 400
    try:
        eligible, skipped = start_shorts(slug, sorted(numbers), max_shorts)
    except Refused as e:
        return jsonify(error=str(e)), e.status
    return jsonify(ok=True, started=eligible, skipped=skipped)


@bp.post("/api/projects/<slug>/shorts/batch/<control>")
def api_shorts_control(slug, control):
    """control: pause | resume | cancel, for the whole Shorts batch."""
    if control not in ("pause", "resume", "cancel"):
        return jsonify(error="Unknown control."), 404
    return batch_do(slug, lambda b: getattr(b, control)(), SHORTS_BATCHES)


@bp.post("/api/projects/<slug>/shorts/batch/chapters/<int:n>/retry")
def api_shorts_retry(slug, n):
    """Run failed chapter n of the Shorts batch again (body {from_start: true}: from its first step)."""
    from_start = bool((request.get_json(silent=True) or {}).get("from_start"))
    return batch_do(slug, lambda b: b.retry(n, from_start=from_start), SHORTS_BATCHES)


@bp.post("/api/projects/<slug>/shorts/batch/chapters/<int:n>/skip")
def api_shorts_skip(slug, n):
    """Give up on failed chapter n of the Shorts batch."""
    return batch_do(slug, lambda b: b.skip(n), SHORTS_BATCHES)

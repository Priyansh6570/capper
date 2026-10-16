"""Routes for render and script jobs and the Claude engine."""
from __future__ import annotations

import json

from flask import Blueprint, Response, jsonify, request

import claude_cli
from services.common import Refused, now_iso, sanitize_chapter, scope_params
from services.editor_state import manifest_panels
from services.jobs import ACTIVE_CHAPTERS, JOBS, JOBS_LOCK, QUEUE_ORDER, _enqueue, jobs_with_eta, new_render_job, new_script_job, stop_running

bp = Blueprint("jobs", __name__)


@bp.post("/api/generate")
def api_generate():
    """Enqueue an audio+video / voice-only / video-only render for a chapter.
    Body: JSON {chapter, what:both|voice|video, project?, ch_no?}. Returns
    immediately with {job_id} - it does NOT run inline or stream; poll
    GET /api/jobs/<job_id> (or GET /api/jobs for the global queue/corner
    widget) for progress. Only one active (queued or running) job per
    chapter at a time; a second request for the SAME chapter is rejected,
    but a DIFFERENT chapter queues behind whatever's already running."""
    data = request.get_json(silent=True) or {}
    what = (data.get("what") or "both").strip().lower()
    try:
        job = new_render_job(sanitize_chapter(data.get("chapter") or ""),
                              what if what in ("both", "voice", "video") else "both", *scope_params())
    except Refused as e:
        return jsonify(error=str(e)), e.status
    _enqueue(job)
    return jsonify(ok=True, job_id=job.id, job=job.public())


@bp.get("/api/jobs/<job_id>/events")
def api_job_events(job_id):
    """Server-sent stream of a script job's events (see Job.emit), replayed from
    Last-Event-ID so a reconnect or a reload picks up where it left off."""
    job = JOBS.get(job_id)
    if job is None:
        return jsonify(error="job not found"), 404
    next_id = int(request.headers.get("Last-Event-ID", -1)) + 1

    def stream():
        nonlocal next_id
        while True:
            with job._events_cond:
                if next_id >= len(job.events):
                    job._events_cond.wait(timeout=15)
                pending = job.events[next_id:]
            if not pending:
                yield ": keepalive\n\n"
            for event in pending:
                yield f"id: {next_id}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                next_id += 1
                if event["type"] == "end":
                    return

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.get("/api/claude/panels")
def api_claude_panels():
    """The chapter's panel ids and strip boxes, for the strip viewer's read marks."""
    chapter_id = sanitize_chapter(request.args.get("chapter", ""))
    return jsonify(panels=[{"id": p.id, "bbox": p.bbox} for p in manifest_panels(chapter_id)])


@bp.get("/api/claude/status")
def api_claude_status():
    return jsonify(claude_cli.cli_status())


@bp.post("/api/script/claude")
def api_script_claude():
    """Enqueue Claude script generation for a project chapter. Body: JSON
    {chapter, project, ch_no}. Returns {job_id}; poll /api/jobs for progress."""
    try:
        job = new_script_job(sanitize_chapter((request.get_json(silent=True) or {}).get("chapter") or ""),
                              *scope_params())
    except Refused as e:
        return jsonify(error=str(e)), e.status
    _enqueue(job)
    return jsonify(ok=True, job_id=job.id, job=job.public())


@bp.get("/api/jobs")
def api_jobs():
    """The full job picture: queued + running (with eta_s, in run order) plus
    a short tail of recently finished jobs. Polled by the global corner
    widget (any screen) and by a chapter editor on open, to reflect the REAL
    state instead of assuming nothing changed while the user was elsewhere."""
    active = jobs_with_eta()
    with JOBS_LOCK:
        finished = sorted(
            (j for j in JOBS.values() if j.status in ("done", "failed", "cancelled")),
            key=lambda j: j.finished_at or "", reverse=True)[:10]
        finished = [j.public() for j in finished]
    return jsonify(jobs=active + finished)


@bp.get("/api/jobs/<job_id>")
def api_job_detail(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify(error="job not found"), 404
        d = job.public()
        if job.status in ("running", "queued"):
            ahead_s = 0.0
            if job.status == "queued":
                for jid in QUEUE_ORDER:
                    if jid == job_id:
                        break
                    j2 = JOBS.get(jid)
                    if j2:
                        ahead_s += j2.estimated_total_s()
                running = next((j for j in JOBS.values() if j.status == "running"), None)
                if running:
                    ahead_s += running.eta_s() or 0.0
            d["eta_s"] = job.eta_s(ahead_s)
    return jsonify(job=d)


@bp.post("/api/jobs/<job_id>/cancel")
def api_job_cancel(job_id):
    """Cancel a queued job (removed before it ever runs) or a running one
    (its subprocess is terminated directly - immediate, not waiting for the
    next log line like the old SSE-disconnect-triggered cancel did)."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify(error="job not found"), 404
        if job.status == "queued":
            job.status, job.ok = "cancelled", False
            job.message = "Cancelled before it started."
            job.finished_at = now_iso()
            try:
                QUEUE_ORDER.remove(job_id)
            except ValueError:
                pass
            ACTIVE_CHAPTERS.discard(job.chapter_id)
            return jsonify(ok=True, job=job.public())
        if job.status == "running":
            stop_running(job)
            return jsonify(ok=True, job=job.public())
    return jsonify(error=f"Job already {job.status}."), 409

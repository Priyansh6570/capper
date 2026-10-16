"""Routes for the project's batch pipeline."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

import batch
import claude_cli
import claude_usage
import preflight
import projects
from services.batching import BATCHES, BATCH_LOCK, batch_do, batch_env, batch_path, job_summary

bp = Blueprint("batch", __name__)


@bp.get("/api/projects/<slug>/batch")
def api_batch(slug):
    """The project's batch (or null) with each chapter's live job, plus subscription usage."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    current = BATCHES.get(slug)
    return jsonify(batch=current.view(job_summary) if current else None, usage=claude_usage.snapshot(),
                   thresholds=projects.load(slug)["settings"]["batch"])


@bp.get("/api/projects/<slug>/batch/preflight")
def api_batch_preflight(slug):
    """Checks to run before a batch of `?chapters=1,2,3` running `?steps=script,export` starts."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    numbers = [int(n) for n in request.args.get("chapters", "").split(",") if n.strip().isdigit()]
    steps = {s for s in request.args.get("steps", "").split(",") if s in batch.LANE_STAGES}
    return jsonify(checks=preflight.check(projects.load(slug), numbers, steps))


@bp.post("/api/projects/<slug>/batch")
def api_batch_start(slug):
    """Body: JSON {plans: {"<chapter>": [step, ...]}}. Runs just those steps, in pipeline order, for
    each chapter, pipelined across chapters. A batch that is already running takes them on as well."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    known = {c["n"] for c in projects.load(slug)["chapters"]}
    try:
        plans = {int(n): [s for s in batch.LANE_STAGES if s in steps]
                 for n, steps in ((request.get_json(silent=True) or {}).get("plans") or {}).items()}
    except (ValueError, TypeError):
        plans = {}
    if not plans or not set(plans) <= known or not all(plans.values()):
        return jsonify(error="Choose steps to run for chapters of this project."), 400
    if any("script" in steps for steps in plans.values()):
        status = claude_cli.cli_status()
        if not status["ready"]:
            return jsonify(error=status["message"]), 409
    with BATCH_LOCK:
        previous = BATCHES.get(slug)
        if previous and previous.active:
            try:
                previous.enqueue(plans)
            except ValueError as e:
                return jsonify(error=str(e)), 409
            return jsonify(ok=True)
        if previous:
            previous.close()
        BATCHES[slug] = batch.Batch.create(batch_path(slug), batch_env(slug), plans)
    return jsonify(ok=True)


@bp.post("/api/projects/<slug>/batch/<control>")
def api_batch_control(slug, control):
    """control: pause | resume | cancel, for the whole batch."""
    if control not in ("pause", "resume", "cancel"):
        return jsonify(error="Unknown control."), 404
    return batch_do(slug, lambda b: getattr(b, control)())


@bp.post("/api/projects/<slug>/batch/chapters/<int:n>/continue")
def api_batch_continue(slug, n):
    """Continue chapter n past the frame review."""
    return batch_do(slug, lambda b: b.continue_review(n))


@bp.post("/api/projects/<slug>/batch/usage")
def api_batch_usage(slug):
    """Body: JSON {choice: 'wait' | 'continue'} for the usage limit the batch is holding at."""
    choice = (request.get_json(silent=True) or {}).get("choice")
    return batch_do(slug, lambda b: b.choose(choice))

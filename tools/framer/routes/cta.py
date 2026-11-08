"""Routes for the call-to-action clip (cta.py): its state, building it, and playing it."""
from __future__ import annotations

import threading

from flask import Blueprint, Response, jsonify, send_from_directory

import cta
import projects
from services.common import sse

bp = Blueprint("cta", __name__)
_BUILDING: set[str] = set()
_LOCK = threading.Lock()


@bp.get("/api/projects/<slug>/cta")
def api_cta(slug):
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    return jsonify(**cta.info(slug), building=slug in _BUILDING, script=cta.load_script(slug))


@bp.get("/api/projects/<slug>/cta_stream")
def cta_stream(slug):
    """Make the project's call-to-action video. EventSource (GET); emits {type:start|log|done|error}."""
    def stream():
        if not projects.exists(slug):
            yield sse({"type": "error", "message": f"No such project: {slug}"})
            return
        with _LOCK:
            if slug in _BUILDING:
                yield sse({"type": "error", "message": "The call-to-action video is already being made."})
                return
            _BUILDING.add(slug)
        try:
            yield sse({"type": "start", "message": "Making the call-to-action video…"})
            for line in cta.build(slug):
                yield sse({"type": "log", "line": line})
            yield sse({"type": "done", "ok": True, "message": "Call-to-action video ready."})
        except RuntimeError as e:
            yield sse({"type": "error", "message": str(e)})
        except Exception as e:  # noqa: BLE001 - report, don't crash the stream
            yield sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            with _LOCK:
                _BUILDING.discard(slug)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.get("/projects/<slug>/cta/cta.mp4")
def cta_file(slug):
    clip = cta.clip_path(slug)
    if not clip.is_file():
        return jsonify(error="No call-to-action video yet."), 404
    return send_from_directory(str(clip.parent), clip.name)

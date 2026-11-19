"""Routes for the call-to-action clip (cta.py): its state, building it, and playing it."""
from __future__ import annotations

import threading

from flask import Blueprint, Response, jsonify, send_from_directory

import cta
import projects
from services.common import sse

bp = Blueprint("cta", __name__)
_BUILDING: set[tuple[str, str]] = set()
_LOCK = threading.Lock()


@bp.get("/api/projects/<slug>/cta")
def api_cta(slug, kind="cta"):
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    return jsonify(**cta.info(slug, kind), building=(slug, kind) in _BUILDING, script=cta.load_script(slug, kind))


@bp.get("/api/projects/<slug>/cta_stream")
def cta_stream(slug, kind="cta"):
    """Make the project's call-to-action video (or, with kind "closure", its closure video). EventSource (GET); emits {type:start|log|done|error}."""
    def stream():
        if not projects.exists(slug):
            yield sse({"type": "error", "message": f"No such project: {slug}"})
            return
        with _LOCK:
            if (slug, kind) in _BUILDING:
                yield sse({"type": "error", "message": "This video is already being made."})
                return
            _BUILDING.add((slug, kind))
        try:
            label = "closure" if kind == "closure" else "call-to-action"
            yield sse({"type": "start", "message": f"Making the {label} video…"})
            for line in cta.build(slug, kind):
                yield sse({"type": "log", "line": line})
            yield sse({"type": "done", "ok": True, "message": f"{label.capitalize()} video ready."})
        except RuntimeError as e:
            yield sse({"type": "error", "message": str(e)})
        except Exception as e:  # noqa: BLE001 - report, don't crash the stream
            yield sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            with _LOCK:
                _BUILDING.discard((slug, kind))

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.get("/projects/<slug>/cta/cta.mp4")
def cta_file(slug, kind="cta"):
    clip = cta.clip_path(slug, kind)
    if not clip.is_file():
        return jsonify(error="No such video yet."), 404
    return send_from_directory(str(clip.parent), clip.name)


# the closure video: the same routes with kind="closure"
bp.add_url_rule("/api/projects/<slug>/closure", "api_closure", api_cta, defaults={"kind": "closure"})
bp.add_url_rule("/api/projects/<slug>/closure_stream", "closure_stream", cta_stream, defaults={"kind": "closure"})
bp.add_url_rule("/projects/<slug>/closure/closure.mp4", "closure_file", cta_file, defaults={"kind": "closure"})

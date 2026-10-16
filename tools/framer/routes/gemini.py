"""Routes for Gemini script generation and its API key."""
from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

import config
import projects
import tones
from config import chapter_output_dir, chapter_work_dir
from services.common import sanitize_chapter, scope_params, sse
from services.gemini import build_gemini_script_stream, clear_gemini_key, get_gemini_key, set_gemini_key

bp = Blueprint("gemini", __name__)


@bp.get("/gemini_script_stream")
def gemini_script_stream():
    """Generate the narration script with the Gemini API from a chapter's
    already-prepared gemini_parts/*.pdf files (see /gemini_parts_stream -
    this route never creates or re-splits them, only reads what's there).

    EventSource (GET) only, params: ?chapter_id=...&project=<slug>&ch_no=<n>.
    Tone/narration-type/style-reference/model come from that PROJECT's saved
    settings (Settings screen), not query params - keeps the pasted style
    reference out of a URL entirely. Emits {type:start|log|progress|done|error}
    exactly like /gemini_parts_stream; `done` carries {script: "<full text>"}."""
    chapter_id = sanitize_chapter(request.args.get("chapter_id", ""))
    slug, ch_no = scope_params()
    work_dir = projects.chapter_work_dir(slug, ch_no) if slug and ch_no else chapter_work_dir(chapter_id)
    output_dir = projects.chapter_output_dir(slug, ch_no) if slug and ch_no else chapter_output_dir(chapter_id)

    gemini_settings: dict = {"tone": tones.DEFAULT_TONE}
    if slug and projects.exists(slug):
        project = projects.load(slug)
        gemini_settings = {**project["settings"]["gemini"], "tone": projects.chapter_tone(project, ch_no)}

    def stream():
        try:
            with config.chapter_scope(work_dir, output_dir):
                if not chapter_id:
                    yield sse({"type": "error", "message": "chapter_id required."})
                    return
                for item in build_gemini_script_stream(chapter_id, gemini_settings):
                    if item["type"] == "result":
                        yield sse({"type": "done", "ok": True, "script": item["script"]})
                    else:
                        yield sse(item)
        except GeneratorExit:
            raise
        except Exception as e:  # noqa: BLE001 - last-resort: still report, don't crash the stream
            yield sse({"type": "error", "message": f"{type(e).__name__}: {e}"})

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@bp.get("/api/gemini/key_status")
def api_gemini_key_status():
    key, source = get_gemini_key()
    return jsonify(set=bool(key), hint=(f"…{key[-4:]}" if len(key) >= 4 else ""), source=source)


@bp.post("/api/gemini/key")
def api_set_gemini_key():
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify(error="Key required."), 400
    source = set_gemini_key(key)
    return jsonify(ok=True, source=source)


@bp.delete("/api/gemini/key")
def api_clear_gemini_key():
    clear_gemini_key()
    return jsonify(ok=True)

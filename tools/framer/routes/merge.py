"""Routes for merged videos and their branded finals."""
from __future__ import annotations

import subprocess

from flask import Blueprint, Response, jsonify, request, send_from_directory

import branding
import projects
from config import NO_CONSOLE, ROOT
from services.common import Refused, sse
from services.jobs import Job, _enqueue
from services.merging import MERGE_JOBS, MERGE_LOCK, _busy, brand_key, merge_slot, prepare_merge

bp = Blueprint("merge", __name__)


@bp.get("/api/projects/<slug>/merge_stream")
def merge_stream(slug):
    """Concatenate several chapters' recap.mp4, in chapter order, into one
    video under projects/<slug>/merged/. EventSource (GET) only:
        ?chapters=1,3,5
    Only chapters whose recap.mp4 exists on disk are accepted. When every input shares 1920x1080 +
    a matching video AND audio codec, the merge is a fast stream copy;
    otherwise every input is scaled/padded to 1920x1080@24fps first (see
    build_merge_cmd_reencode) so a mismatched render can't corrupt the merge.
    Emits {type:start|log|done|error}; `done` carries the merged file's URL."""
    raw = (request.args.get("chapters") or "").strip()
    try:
        chapters = sorted({int(x) for x in raw.split(",") if x.strip()})
    except ValueError:
        chapters = []

    def stream():
        if not projects.exists(slug):
            yield sse({"type": "error", "message": f"No such project: {slug}"})
            return
        if len(chapters) < 2:
            yield sse({"type": "error", "message": "Select at least 2 chapters with a video to merge."})
            return

        proc = None
        try:
            with merge_slot(slug):
                yield sse({"type": "start", "message": f"Checking {len(chapters)} chapter(s)…"})
                plan = prepare_merge(slug, chapters)
                for note in plan.notes:
                    yield sse({"type": "log", "line": note})
                yield sse({"type": "log", "line": "$ " + " ".join(plan.cmd)})
                proc = subprocess.Popen(plan.cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        text=True, bufsize=1, encoding="utf-8", errors="replace",
                                        **NO_CONSOLE)
                with MERGE_LOCK:
                    MERGE_JOBS[slug] = proc
                for line in proc.stdout:
                    yield sse({"type": "log", "line": line.rstrip("\n")})
                proc.wait()

                if proc.returncode == 0 and plan.out.is_file():
                    yield sse({"type": "done", "ok": True, "video": f"/projects/{slug}/merged/{plan.out.name}",
                               "message": f"Merged {len(chapters)} chapter(s) -> {plan.out.name}"})
                else:
                    yield sse({"type": "done", "ok": False, "message": f"ffmpeg exited {proc.returncode}."})
        except Refused as e:
            yield sse({"type": "error", "message": str(e)})
        except GeneratorExit:                              # client disconnected / Stop
            if proc and proc.poll() is None:
                proc.terminate()
            raise
        except Exception as e:  # noqa: BLE001 - report, don't crash the stream
            yield sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            if proc and proc.poll() is None:
                proc.terminate()

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.get("/projects/<slug>/merged/<path:name>")
def merged_file(slug, name):
    """Serve a merged video for in-page play/download (Range-friendly, same
    as /output/<chapter_id>/<name>)."""
    d = projects.merged_dir(slug)
    if not (d / name).exists():
        return jsonify(error=f"{name} not found for this project."), 404
    return send_from_directory(str(d), name)


@bp.get("/api/projects/<slug>/merges")
def api_list_merges(slug):
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    return jsonify(merges=branding.list_merges(slug), folder=str(projects.merged_dir(slug)),
                   branding=projects.load(slug)["settings"]["branding"],
                   default_clips={k: p.name for k, p in branding.DEFAULT_CLIPS.items()})


@bp.get("/api/projects/<slug>/branding/<kind>")
def api_branding_clip(slug, kind):
    """The clip that will actually be used for `kind` (custom upload or bundled default)."""
    if kind not in branding.CLIP_KINDS or not projects.exists(slug):
        return jsonify(error="unknown project or clip"), 404
    try:
        clip = branding.clip_source(slug, kind)
    except FileNotFoundError as e:
        return jsonify(error=str(e)), 404
    return send_from_directory(str(clip.parent), clip.name)


@bp.post("/api/projects/<slug>/merges/<name>/final")
def api_render_final(slug, name):
    """Queue the branded version of a merged video (intro/outro per the project's
    branding settings). Writes `<merge>_branded.mp4`; the raw merge is untouched."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    try:
        raw = branding.merge_path(slug, name)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if not raw.is_file():
        return jsonify(error=f"{name} not found."), 404
    settings = projects.load(slug)["settings"]["branding"]
    if not any(settings[k]["enabled"] for k in branding.CLIP_KINDS):
        return jsonify(error="Turn on the intro or the outro first."), 400
    key = brand_key(slug, name)
    if _busy(key):
        return jsonify(error="A final render is already queued or running for this video."), 409
    job = Job(key, "brand", slug, None, 0, target=name)
    job.append_log(f"Queued final render of {name}…")
    _enqueue(job)
    return jsonify(ok=True, job_id=job.id, job=job.public())


@bp.delete("/api/projects/<slug>/merges/<name>")
def api_delete_merge(slug, name):
    """Delete a merged video and its branded file, or only the branded file (?final=1)."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    if _busy(brand_key(slug, name)):
        return jsonify(error="A final render is in progress for this video."), 409
    try:
        branding.delete_merge(slug, name, final_only=request.args.get("final") == "1")
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)

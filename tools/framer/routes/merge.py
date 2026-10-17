"""Routes for merged videos and their branded finals."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, send_from_directory

import branding
import chapter_backup
import gpu
import projects
from config import NO_CONSOLE, ROOT
from services.common import sse
from services.jobs import Job, _enqueue
from services.merging import MERGE_JOBS, MERGE_LOCK, _busy, brand_key, build_merge_cmd_copy, build_merge_cmd_reencode, write_concat_list

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

        with MERGE_LOCK:
            busy = slug in MERGE_JOBS
            if not busy:
                MERGE_JOBS[slug] = None
        if busy:
            yield sse({"type": "error", "message": "A merge is already running for this project."})
            return

        proc = None
        try:
            yield sse({"type": "start", "message": f"Checking {len(chapters)} chapter(s)…"})
            paths: list[Path] = []
            for n in chapters:
                ch = projects.get_chapter(projects.load(slug), n)
                if ch is None:
                    yield sse({"type": "error", "message": f"No chapter {n} in this project."})
                    return
                try:
                    chapter_backup.restore(slug, n, ["video"])   # a video cleared after a Drive backup comes back
                except chapter_backup.BackupUnavailable as e:
                    yield sse({"type": "error", "message": str(e)})
                    return
                video = projects.chapter_video_path(slug, n)
                if not video.is_file():
                    yield sse({"type": "error", "message": f"Chapter {n} has no rendered recap.mp4."})
                    return
                paths.append(video)

            yield sse({"type": "log", "line": f"Probing {len(paths)} video(s)…"})
            infos = []
            for n, p in zip(chapters, paths):
                info = branding.probe(p)
                if info is None:
                    yield sse({"type": "error", "message": f"Could not read video info for chapter {n}."})
                    return
                infos.append(info)
                v, a = info["video"], info.get("audio") or {}
                yield sse({"type": "log",
                            "line": f"  ch {n}: {v.get('width')}x{v.get('height')} {v.get('codec_name')} "
                                    f"/ audio {a.get('codec_name', '?')}@{a.get('sample_rate', '?')}"})

            uniform = (
                len({(i["video"].get("width"), i["video"].get("height"), i["video"].get("codec_name"))
                     for i in infos}) == 1
                and len({(i.get("audio") or {}).get("codec_name") for i in infos}) == 1
                and len({(i.get("audio") or {}).get("sample_rate") for i in infos}) == 1
            )
            out_dir = projects.merged_dir(slug)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            name = f"merged_{stamp}_ch" + "-".join(str(n) for n in chapters) + ".mp4"
            out = out_dir / name
            ffmpeg = gpu.ffmpeg_path()

            if uniform:
                yield sse({"type": "log", "line": "All inputs share one size and codec - "
                                                    "fast stream-copy merge (no re-encode)."})
                list_file = out_dir / "_concat_list.txt"
                write_concat_list(paths, list_file)
                cmd = build_merge_cmd_copy(ffmpeg, list_file, out)
            else:
                size = (infos[0]["video"]["width"], infos[0]["video"]["height"])
                yield sse({"type": "log",
                            "line": f"Inputs differ in resolution/codec - normalizing every clip to "
                                    f"{size[0]}x{size[1]}@24fps before merging (slower)."})
                cmd = build_merge_cmd_reencode(ffmpeg, size, paths, out)

            yield sse({"type": "log", "line": "$ " + " ".join(cmd)})
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    text=True, bufsize=1, encoding="utf-8", errors="replace",
                                    **NO_CONSOLE)
            with MERGE_LOCK:
                MERGE_JOBS[slug] = proc
            for line in proc.stdout:
                yield sse({"type": "log", "line": line.rstrip("\n")})
            proc.wait()

            if proc.returncode == 0 and out.is_file():
                url = f"/projects/{slug}/merged/{name}"
                yield sse({"type": "done", "ok": True, "video": url,
                            "message": f"Merged {len(chapters)} chapter(s) -> {name}"})
            else:
                yield sse({"type": "done", "ok": False,
                            "message": f"ffmpeg exited {proc.returncode}."})
        except GeneratorExit:                              # client disconnected / Stop
            if proc and proc.poll() is None:
                proc.terminate()
            raise
        except Exception as e:  # noqa: BLE001 - report, don't crash the stream
            yield sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
            with MERGE_LOCK:
                MERGE_JOBS.pop(slug, None)

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

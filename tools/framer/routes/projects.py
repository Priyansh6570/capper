"""Routes for projects: listing, chapters, settings, assets, defaults, previews."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PIL import Image
from flask import Blueprint, Response, jsonify, request, send_file, send_from_directory

import app_settings
import bulk_download
import config
import project_defaults
import projects
import series_meta
import sites
import totals
from services.batching import BATCHES, chapter_thumb
from services.downloads import _download_chapter
from services.jobs import JOBS
from services.project_ops import add_chapter_details, placeholder_frame, png_response, stream_task
from stages import s7_assemble as s7, tts_kokoro

bp = Blueprint("projects", __name__)


# --------------------------------------------------------------------------- #
# project system - list/create/open projects, chapter status, media settings +
# live previews. See tools/framer/projects.py (storage) and tools/framer/sites/
# (the best-effort per-site series scrapers). Chapter-scoped routes above are
# entered via the before_request hook near the top of this file whenever a
# request carries `project` + `ch_no`.
# --------------------------------------------------------------------------- #
@bp.get("/api/projects")
def api_list_projects():
    return jsonify(projects=projects.list_projects())


@bp.post("/api/projects")
def api_create_project():
    """Body: JSON {name, url}. Creates the project, then tries to auto-fetch
    series details (title, chapter count, chapter list) from whichever site
    adapter (see tools/framer/sites/) matches `url`. NEVER blocks on a failed
    fetch - `fetch.ok` tells the UI whether to fall back to the manual "enter
    chapter range 1..N" input."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    url = (data.get("url") or "").strip()
    if not name:
        return jsonify(error="Project name required."), 400
    project = project_defaults.create_project(name, url)

    adapter = sites.detect(url) if url else None
    fetched = adapter.fetch_series(url) if adapter else None
    if adapter:
        project["site"] = adapter.SITE_NAME
    if fetched:
        project["series_title"] = fetched["title"]
        project["source"] = "scraped"
        projects.fill_chapters(project, fetched["chapters"])
        series_meta.save(project, fetched)
        fetch_result = {"ok": True, "title": fetched["title"], "total": fetched["total"]}
    else:
        fetch_result = {"ok": False, "reason": "Couldn't read the series page - enter "
                                                "the chapter range manually."}
    projects.save(project)

    return jsonify(slug=project["slug"], project=project, fetch=fetch_result)


@bp.get("/api/projects/<slug>/cover")
def api_project_cover(slug):
    path = series_meta.cover_path(slug)
    return send_file(path, mimetype="image/jpeg", max_age=0) if path.is_file() else (jsonify(error="No cover."), 404)


@bp.get("/api/projects/<slug>/missing_chapters")
def api_missing_chapters(slug):
    """How many of the project's chapters are not yet stored in its project folder."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    return jsonify(count=len(bulk_download.missing_chapters(slug)), total=len(projects.load(slug)["chapters"]))


@bp.get("/api/projects/<slug>/download_stream")
def api_bulk_download(slug):
    """Download the next `count` chapters not yet stored in the project folder. Frames: log, progress
    {chapter, position, total}; done {requested, failed:[{chapter, error}]}."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    project = projects.load(slug)
    if not project["site"]:
        return jsonify(error="This project has no remembered source to download from."), 400
    try:
        count = int(request.args.get("count", ""))
    except ValueError:
        return jsonify(error="count must be a number of chapters."), 400

    def task(emit, stop):
        def log(line):
            emit({"type": "log", "line": line})

        def download_one(n: int) -> None:
            with config.chapter_scope(projects.chapter_work_dir(slug, n), projects.chapter_output_dir(slug, n)):
                _download_chapter(project["url"], str(n), f"{slug}_ch{n}", slug, str(n), log)

        def progress(chapter: int, position: int, total: int) -> None:
            emit({"type": "progress", "chapter": chapter, "position": position, "total": total})

        return bulk_download.run(slug, max(count, 0), download_one, progress, stop.is_set)

    return stream_task(task)


def _app_settings_view():
    folder, cache = app_settings.projects_folder(), app_settings.model_cache()
    in_use = os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface")
    return jsonify(projects_folder=str(folder) if folder else "", model_cache=str(cache) if cache else "",
                   model_cache_in_use=in_use)


@bp.get("/api/app_settings")
def api_get_app_settings():
    return _app_settings_view()


@bp.post("/api/app_settings")
def api_set_app_settings():
    """Body: JSON with `projects_folder` and/or `model_cache`; an empty string goes back to the default."""
    data = request.get_json(silent=True) or {}
    try:
        if "projects_folder" in data:
            app_settings.set_projects_folder(data["projects_folder"] or "")
        if "model_cache" in data:
            app_settings.set_model_cache(data["model_cache"] or "")
    except OSError as e:
        return jsonify(error=f"Couldn't use that folder: {e}"), 400
    return _app_settings_view()


@bp.get("/api/projects/<slug>")
def api_get_project(slug):
    """The project with every chapter's stored fields and status. The per-chapter detail (tone,
    narration estimate, video length, steps, thumbnail) is costly to gather from disk, so it is added
    only to the first `limit` chapters - the page of the chapter list being shown."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    project = projects.derive_all(slug)
    limit = request.args.get("limit", type=int)
    project["detail_limit"] = limit if limit is not None else len(project["chapters"])
    add_chapter_details(project, project["chapters"][:project["detail_limit"]])
    project["claude_usage"] = totals.claude_usage(slug)
    return jsonify(project=project)


@bp.get("/api/projects/<slug>/chapter_details")
def api_chapter_details(slug):
    """The detail api_get_project adds to a page of chapters, for the chapters in `numbers` (comma
    separated), so a bulk action on chapters that are not on the page can still see their steps."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    try:
        wanted = {int(n) for n in request.args.get("numbers", "").split(",") if n}
    except ValueError:
        return jsonify(error="numbers must be chapter numbers."), 400
    project = projects.derive_all(slug)
    chapters = [ch for ch in project["chapters"] if ch["n"] in wanted]
    add_chapter_details(project, chapters)
    return jsonify(chapters=chapters)


@bp.get("/api/projects/<slug>/chapters/<int:n>/thumb")
def api_chapter_thumb(slug, n):
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    thumb = chapter_thumb(slug, n)
    return send_file(thumb, mimetype="image/jpeg", max_age=0) if thumb else (jsonify(error="No frame yet."), 404)


@bp.get("/api/projects/<slug>/export")
def api_export_project(slug):
    """Zip the whole project folder (downloads, frames, audio, renders, assets,
    settings) for backup or transfer. `?videos=0` leaves out rendered videos."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        projects.export_archive(slug, Path(tmp), include_videos=request.args.get("videos") != "0")
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    resp = send_file(tmp, as_attachment=True, download_name=f"{slug}.recapper.zip",
                     mimetype="application/zip")
    resp.call_on_close(lambda: Path(tmp).unlink(missing_ok=True))
    return resp


@bp.post("/api/projects/import")
def api_import_project():
    """Multipart: {file} - a zip made by the export route. Always creates a new
    project (renamed if the slug is taken), never overwrites one."""
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify(error="file required"), 400
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        upload.save(tmp)
        project = projects.import_archive(Path(tmp))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    finally:
        Path(tmp).unlink(missing_ok=True)
    return jsonify(slug=project["slug"], project=project)


@bp.delete("/api/projects/<slug>")
def api_delete_project(slug):
    """Body: JSON {confirm} - must be the project's exact name. Removes the project's files from every
    location (see projects.delete)."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    if (request.get_json(silent=True) or {}).get("confirm") != projects.load(slug)["name"]:
        return jsonify(error="Type the project's exact name to confirm."), 400
    running = BATCHES.get(slug)
    if (running and running.active) or any(j.slug == slug and j.status in ("queued", "running") for j in JOBS.values()):
        return jsonify(error="This project is busy - cancel its pipeline and jobs first."), 409
    try:
        projects.delete(slug)
    except OSError as e:
        return jsonify(error=str(e)), 500
    BATCHES.pop(slug, None)
    return jsonify(ok=True)


@bp.post("/api/projects/<slug>/chapters")
def api_set_chapters(slug):
    """Body: JSON {from, to} for the manual fallback range, or {refetch:true} to
    retry the source-site scrape."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    data = request.get_json(silent=True) or {}
    project = projects.load(slug)

    if data.get("refetch"):
        adapter = sites.detect(project["url"])
        fetched = adapter.fetch_series(project["url"]) if adapter else None
        if not fetched:
            return jsonify(error="Couldn't read the series page.", ok=False), 200
        project["series_title"] = fetched["title"]
        project["source"] = "scraped"
        projects.fill_chapters(project, fetched["chapters"])
        series_meta.save(project, fetched)
        projects.save(project)
        return jsonify(ok=True, project=project)

    try:
        start, end = int(data.get("from")), int(data.get("to"))
    except (TypeError, ValueError):
        return jsonify(error="from and to must be chapter numbers."), 400
    if start < 1 or end < start:
        return jsonify(error="Invalid chapter range."), 400
    project["source"] = "manual"
    projects.manual_range(project, start, end)
    projects.save(project)
    return jsonify(ok=True, project=project)


@bp.post("/api/projects/<slug>/chapters/<n>/status")
def api_set_chapter_status(slug, n):
    """Body: JSON {status, force?:bool}. `force` is used by "Mark complete" and
    its reset action, which are manual overrides outside the normal ranking."""
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    try:
        ch = projects.set_status(slug, n, status, force=bool(data.get("force")))
    except (ValueError, KeyError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, chapter=ch)


@bp.post("/api/projects/<slug>/chapters/<int:n>/tone")
def api_set_chapter_tone(slug, n):
    """Body: JSON {tone}: one of tones.TONES, or null to follow the project's default tone."""
    try:
        ch = projects.set_chapter_tone(slug, n, (request.get_json(silent=True) or {}).get("tone"))
    except (ValueError, KeyError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, chapter=ch)


@bp.get("/api/tts/kokoro/voices")
def api_kokoro_voices():
    return jsonify(voices=tts_kokoro.VOICES)


@bp.get("/api/tts/kokoro/preview")
def api_kokoro_preview():
    """A short sample of the Kokoro voice in `?voice=`, as a wav."""
    voice = request.args.get("voice", "")
    if voice not in tts_kokoro.VOICES:
        return jsonify(error="Unknown Kokoro voice."), 400
    try:
        return Response(tts_kokoro.sample_wav(voice), mimetype="audio/wav")
    except ImportError:
        return jsonify(error="Kokoro is not installed. Run setup.bat again, or: pip install kokoro misaki[en]"), 409


@bp.get("/api/projects/<slug>/settings")
def api_get_settings(slug):
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    return jsonify(settings=projects.load(slug)["settings"])


@bp.post("/api/projects/<slug>/settings")
def api_set_settings(slug):
    """Body: JSON settings block (partial - deep-merged). Clamps sliders and
    rewrites media_settings.json for every chapter that already has a work
    dir, so a saved change reaches renders immediately."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    data = request.get_json(silent=True) or {}
    project = projects.update_settings(slug, data)
    return jsonify(ok=True, settings=project["settings"])


@bp.post("/api/projects/<slug>/defaults")
def api_save_defaults(slug):
    """Make this project's settings (and the custom files they use) the defaults for new projects."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    project_defaults.save_from(slug)
    return jsonify(ok=True)


@bp.get("/api/defaults/changes")
def api_defaults_changes():
    """What applying the default settings would change, for `?project=<slug>` or every project."""
    slugs = [request.args["project"]] if request.args.get("project") else [p["slug"] for p in projects.list_projects()]
    found = [{"slug": slug, "name": projects.load(slug)["name"], "changes": project_defaults.changes(slug)}
             for slug in slugs if projects.exists(slug)]
    return jsonify(projects=[p for p in found if p["changes"]])


@bp.post("/api/defaults/apply")
def api_apply_defaults():
    """Body: JSON {projects: [slug, ...]}. Applies the default settings to each."""
    slugs = (request.get_json(silent=True) or {}).get("projects") or []
    if not slugs or not all(projects.exists(slug) for slug in slugs):
        return jsonify(error="Choose projects to apply the defaults to."), 400
    for slug in slugs:
        project_defaults.apply(slug)
    return jsonify(ok=True)


@bp.get("/api/background_options")
def api_background_options():
    """The ready-made background videos; `?project=<slug>` also says which one that project uses."""
    slug = request.args.get("project")
    return jsonify(options=project_defaults.background_options(),
                   current=project_defaults.current_background_option(slug) if slug and projects.exists(slug) else None)


@bp.get("/api/background_options/<option_id>")
def api_background_option_video(option_id):
    path = project_defaults.background_option(option_id)
    return send_file(path, conditional=True) if path else (jsonify(error="No such background video."), 404)


@bp.post("/api/projects/<slug>/background")
def api_select_background(slug):
    """Body: JSON {option}. Use that ready-made video as the project's background."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    try:
        project_defaults.select_background(slug, (request.get_json(silent=True) or {}).get("option") or "")
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@bp.post("/api/projects/<slug>/asset")
def api_upload_asset(slug):
    """Multipart: {kind: music|voice|watermark|background|intro|outro, file}. Saves the
    file under projects/<slug>/assets/ and points the matching settings field
    at it; does NOT switch mode/enabled by itself (stays whatever the
    settings panel already has - selecting "custom" is a separate action)."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    kind = (request.form.get("kind") or "").strip()
    file_storage = request.files.get("file")
    if not file_storage or not file_storage.filename:
        return jsonify(error="file required"), 400
    try:
        rel_path = projects.save_asset(slug, kind, file_storage.filename, file_storage)
    except ValueError as e:
        return jsonify(error=str(e)), 400

    project = projects.load(slug)
    if kind == "music":
        project["settings"]["music"]["file"] = rel_path
    elif kind == "voice":
        project["settings"]["voice"]["file"] = rel_path
    elif kind == "watermark":
        project["settings"]["watermark"]["file"] = rel_path
    elif kind == "background":
        project["settings"]["background"]["file"] = rel_path
    else:
        project["settings"]["branding"][kind]["file"] = rel_path
    projects.save(project)
    projects.write_all_media_snapshots(slug)
    return jsonify(ok=True, path=rel_path, name=Path(rel_path).name)


@bp.delete("/api/projects/<slug>/asset/<kind>")
def api_delete_asset(slug, kind):
    """Revert `kind` to its default (no file)."""
    if kind not in projects.VALID_ASSET_KINDS:
        return jsonify(error=f"unknown asset kind: {kind}"), 400
    asset = projects.find_asset(slug, kind)
    if asset:
        asset.unlink(missing_ok=True)
    project = projects.load(slug)
    if kind == "music":
        project["settings"]["music"] = {"mode": "default", "file": None}
    elif kind == "voice":
        project["settings"]["voice"] = {"mode": "default", "file": None}
    elif kind == "watermark":
        project["settings"]["watermark"]["file"] = None
    elif kind == "background":
        project["settings"]["background"] = {"mode": "blur", "file": None, "dim": 0.85}
    else:
        project["settings"]["branding"][kind]["file"] = None
    projects.save(project)
    projects.write_all_media_snapshots(slug)
    return jsonify(ok=True)


@bp.get("/api/projects/<slug>/asset/<kind>")
def api_get_asset(slug, kind):
    asset = projects.find_asset(slug, kind)
    if not asset:
        return jsonify(error=f"No {kind} asset saved for this project."), 404
    return send_from_directory(str(asset.parent), asset.name)


@bp.get("/api/projects/<slug>/preview/watermark.png")
def api_preview_watermark(slug):
    """Query: enabled, type, text, opacity, size, spacing, angle. An image-type
    watermark uses the already-saved asset (see /asset).

    BUG FIX: this used to build a non-empty `cfg` dict regardless of the
    "Enabled" checkbox and never looked at it, so the preview always showed
    the watermark tiled even when it was OFF - the actual render (via
    media_settings_snapshot(), which correctly nulls the whole watermark
    block when disabled) would then have no watermark at all, exactly
    matching the "preview shows it, video doesn't" symptom. The preview now
    mirrors the real gate: no watermark in the preview when disabled."""
    enabled = (request.args.get("enabled", "1")).strip().lower() not in ("0", "false", "")
    sample = placeholder_frame(slug)

    wm = None
    if enabled:
        cfg = {
            "type": request.args.get("type", "text"),
            "text": request.args.get("text", ""),
            "opacity": request.args.get("opacity", 0.12),
            "size": request.args.get("size", 0.12),
            "spacing": request.args.get("spacing", 1.0),
            "angle": request.args.get("angle", -30),
        }
        watermark_asset = projects.find_asset(slug, "watermark")
        cfg["file"] = str(watermark_asset) if watermark_asset else None
        wm = s7._build_watermark_layer({"watermark": cfg})

    # wm goes to the BACKGROUND layer only (see _compose_still), matching the
    # real render - so this preview accurately shows the watermark hidden
    # under the sample frame and visible only around/behind it.
    still = s7._compose_still([str(sample)], wm=wm)
    return png_response(still.resize((960, 540), Image.LANCZOS))


@bp.get("/api/projects/<slug>/preview/background.png")
def api_preview_background(slug):
    """Query: dim. Uses the already-saved background asset (see /asset)."""
    bg_asset = projects.find_asset(slug, "background")
    dim = request.args.get("dim", 0.85)
    bg = s7._load_background({"background": {"file": str(bg_asset) if bg_asset else None,
                                              "dim": dim}})
    sample = placeholder_frame(slug)
    still = s7._compose_still([str(sample)], bg)
    return png_response(still.resize((960, 540), Image.LANCZOS))

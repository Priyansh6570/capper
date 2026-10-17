"""Routes for backing a project's chapter files up to its Drive folder, clearing the local copies,
and restoring them. The logic is in chapter_backup.py."""
from __future__ import annotations

import json

from flask import Blueprint, jsonify, request

import branding
import chapter_backup
import projects
from services.project_ops import stream_task

bp = Blueprint("backup", __name__)


def _numbers(raw) -> list[int]:
    out = []
    for part in raw if isinstance(raw, list) else str(raw or "").split(","):
        try:
            out.append(int(part))
        except (TypeError, ValueError):
            continue
    return out


def _kinds(raw) -> list[str]:
    return [k for k in (raw if isinstance(raw, list) else str(raw or "").split(",")) if k in chapter_backup.KINDS]


def _busy_chapters(slug: str) -> set[int]:
    """Chapters still going through a live batch: their files are in use, so they are never cleared."""
    try:
        batch = json.loads((projects.local_dir(slug) / "batch.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if batch.get("status") not in ("running", "paused"):
        return set()
    return {c["n"] for c in batch.get("chapters", []) if c.get("state") not in ("done", "failed", "cancelled")}


@bp.get("/api/projects/<slug>/backup")
def api_backup_status(slug):
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    project = projects.load(slug)
    shared = chapter_backup.is_shared(slug)
    titles = {c["n"]: c.get("title") or f"Chapter {c['n']}" for c in project["chapters"]}
    chapters = chapter_backup.overview(slug, list(titles)) if shared else []
    for entry in chapters:
        entry["title"] = titles[entry["n"]]
    merges = branding.list_merges(slug) if shared else []
    merged_bytes = sum(m["size"] + (m["final"] or {}).get("size", 0) for m in merges)
    return jsonify(
        shared=shared, folder=str(projects.project_dir(slug)), busy=sorted(_busy_chapters(slug)),
        kinds=[{"key": k.key, "label": k.label, "hint": k.hint, "clearable": k.clearable, "default": k.default}
               for k in chapter_backup.KINDS.values()],
        chapters=chapters, merged={"count": len(merges), "bytes": merged_bytes})


@bp.get("/api/projects/<slug>/backup_stream")
def api_backup_stream(slug):
    """EventSource (GET): copy the chosen kinds of the chosen chapters into the project's Drive folder."""
    if not projects.exists(slug) or not chapter_backup.is_shared(slug):
        return jsonify(error="This project isn't stored in a Drive folder."), 400
    numbers, kinds = _numbers(request.args.get("chapters")), _kinds(request.args.get("kinds"))
    if not numbers or not kinds:
        return jsonify(error="Pick at least one chapter and one kind of file."), 400

    def task(emit, stop):
        copied = copied_bytes = 0
        steps = len(numbers) * len(kinds)
        step = 0
        for n in numbers:
            for kind in kinds:
                if stop.is_set():
                    return {"copied": copied, "bytes": copied_bytes, "stopped": True}
                step += 1
                emit({"type": "progress", "chapter": n, "kind": kind, "step": step, "steps": steps})
                result = chapter_backup.backup_kind(slug, n, kind, stop=stop)
                copied += result["copied"]
                copied_bytes += result["bytes"]
        return {"copied": copied, "bytes": copied_bytes, "stopped": False}

    return stream_task(task)


@bp.post("/api/projects/<slug>/backup_clear")
def api_backup_clear(slug):
    """Body: JSON {chapters, kinds}. Deletes the verified, backed-up files from this PC."""
    if not projects.exists(slug) or not chapter_backup.is_shared(slug):
        return jsonify(error="This project isn't stored in a Drive folder."), 400
    data = request.get_json(silent=True) or {}
    numbers, kinds = _numbers(data.get("chapters")), _kinds(data.get("kinds"))
    busy = _busy_chapters(slug)
    freed, skipped = 0, []
    for n in numbers:
        for kind in kinds:
            if n in busy:
                skipped.append({"n": n, "kind": kind, "reason": "it is being processed right now"})
                continue
            try:
                freed += chapter_backup.clear_kind(slug, n, kind)
            except ValueError as e:
                skipped.append({"n": n, "kind": kind, "reason": str(e)})
    return jsonify(ok=True, freed=freed, skipped=skipped)


@bp.post("/api/projects/<slug>/chapters/<n>/restore")
def api_backup_restore(slug, n):
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    try:
        restored = chapter_backup.restore(slug, n)
    except chapter_backup.BackupUnavailable as e:
        return jsonify(error=str(e)), 503
    return jsonify(ok=True, restored=restored)

"""Routes for a project's media library."""
from __future__ import annotations

from flask import Blueprint, jsonify, request, send_from_directory

import branding
import library
import projects
from services.merging import _busy

bp = Blueprint("library", __name__)


# --------------------------------------------------------------------------- #
# media library: sizes per project / chapter / file type, and deleting whole
# regenerable types. Only types known to library.KINDS are reachable here; the
# work itself (script, mapping, editor state, settings, uploads) is not.
# --------------------------------------------------------------------------- #
@bp.get("/api/projects/<slug>/library")
def api_library(slug):
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    merges = branding.list_merges(slug)
    merged_bytes = sum(m["size"] + (m["final"] or {}).get("size", 0) for m in merges)
    return jsonify(**library.overview(slug, merged_bytes), merges=merges)


@bp.get("/api/projects/<slug>/library/<int:n>")
def api_library_chapter(slug, n):
    if not projects.exists(slug) or projects.get_chapter(projects.load(slug), n) is None:
        return jsonify(error="No such chapter."), 404
    return jsonify(library.chapter_detail(slug, n))


@bp.get("/api/projects/<slug>/library/<int:n>/file")
def api_library_file(slug, n):
    path = library.servable_file(slug, n, request.args.get("path", ""))
    if path is None:
        return jsonify(error="No such file."), 404
    return send_from_directory(str(path.parent), path.name)


@bp.post("/api/projects/<slug>/library/delete")
def api_library_delete(slug):
    """Body: JSON {chapters:[n...], kind, dry_run?}. dry_run reports what would be
    freed; otherwise deletes that type across the chapters and returns each
    chapter's status re-derived from disk."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    if kind not in library.KINDS:
        return jsonify(error=f"Unknown file type: {kind}"), 400
    project = projects.load(slug)
    known = {c["n"] for c in project["chapters"]}
    try:
        chapters = sorted({int(n) for n in data.get("chapters") or []})
    except (TypeError, ValueError):
        return jsonify(error="chapters must be chapter numbers."), 400
    if not chapters or not known.issuperset(chapters):
        return jsonify(error="Pick at least one chapter of this project."), 400
    if data.get("dry_run"):
        return jsonify(library.plan_delete(slug, chapters, kind))
    busy = [n for n in chapters if _busy(f"{slug}_ch{n}")]
    if busy:
        return jsonify(error="Chapter(s) " + ", ".join(map(str, busy)) +
                             " have a render in progress; wait for it or stop it first."), 409
    freed, locked = library.delete_kind(slug, chapters, kind)
    statuses = {str(n): projects.derive_status(slug, n)["status"] for n in chapters}
    if locked:
        return jsonify(error=f"{locked} file(s) are in use and were kept - close any open "
                             "player and try again.", freed=freed, statuses=statuses), 409
    return jsonify(ok=True, freed=freed, statuses=statuses)

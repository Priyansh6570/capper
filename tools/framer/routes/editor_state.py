"""Routes for the editor's saved state and a chapter's source status."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from atomic_io import atomic_write_json
from config import chapter_output_dir
from services.chapter_files import downloaded_pdf, ensure_dir, existing_gemini_parts, gemini_parts_dir, pdf_page_count, preview_payload
from services.common import now_iso, sanitize_chapter, scope_params
from services.editor_state import editor_state_path, read_editor_state, resolve_editor_state, state_from_narration
from services.jobs import _video_url

bp = Blueprint("editor_state", __name__)


@bp.post("/editor_state/save")
def save_editor_state():
    """Persist the FULL working state (lines, frames, modes, durations, strip ref).

    Body: JSON {chapter_id, recovery?:bool, state:{...}, project?, ch_no?}.
    `project`/`ch_no` are ONLY the chapter-scope params (see scope_params) -
    the state blob itself is carried under `state`, deliberately NOT `project`,
    so it can never collide with the scope param of the same request. `recovery`
    writes the periodic autosave (recovery.json) so a closed tab never loses
    work; a normal save writes editor_state.json. The blob is stored verbatim
    plus a stamp.
    """
    data = request.get_json(silent=True) or {}
    chapter_id = sanitize_chapter(data.get("chapter_id") or "")
    state = data.get("state")
    if not chapter_id or not isinstance(state, dict):
        return jsonify(error="chapter_id and state required"), 400
    recovery = bool(data.get("recovery"))
    state = dict(state)
    state["chapter_id"] = chapter_id
    state["saved_at"] = now_iso()
    path = editor_state_path(chapter_id, recovery)
    # Atomic + backed up (see atomic_io.py) - this is the live boxing/script
    # session itself; a half-written editor_state.json/recovery.json from a
    # crash mid-save is exactly the kind of loss this guards against.
    atomic_write_json(ensure_dir(path.parent) / path.name, state)
    return jsonify(ok=True, path=str(path), saved_at=state["saved_at"])


@bp.get("/editor_state/load")
def load_editor_state():
    """Restore a saved session: returns {state, preview}.

    Query: chapter, recovery?(0/1), narration?(0/1: the Claude pre-fill, regardless of newer saves). `preview` is the same payload /load_pdf returns
    (rebuilt from the persisted tiles, NO re-stitch) so the strip is shown again;
    it is null if the chapter has no stitched strip on disk.
    """
    chapter_id = sanitize_chapter(request.args.get("chapter", ""))
    if request.args.get("recovery") in ("1", "true", "yes"):
        state = read_editor_state(editor_state_path(chapter_id, True))
    elif request.args.get("narration") in ("1", "true", "yes"):
        state = state_from_narration(chapter_id)
    else:
        state = resolve_editor_state(chapter_id)
    if state is None:
        return jsonify(error=f"No saved editor state for {chapter_id}."), 404
    return jsonify(state=state, preview=preview_payload(chapter_id))


@bp.get("/editor_state/status")
def editor_state_status():
    """Report whether a chapter has a saved state / newer recovery (for the
    'restore unsaved work?' prompt). Returns {project:{exists,mtime},
    recovery:{exists,mtime}}."""
    chapter_id = sanitize_chapter(request.args.get("chapter", ""))
    out = {}
    for key, recovery in (("project", False), ("recovery", True)):
        p = editor_state_path(chapter_id, recovery)
        out[key] = {"exists": p.exists(), "mtime": (p.stat().st_mtime if p.exists() else 0)}
    return jsonify(**out)


@bp.get("/chapter_source_status")
def chapter_source_status():
    """The Chapter Source / Stitch Strip panels' TRUE state, derived straight
    from what's actually on disk for `chapter` - NEVER from editor_state.json,
    which only ever held the framer's own boxing data (lines/frames) and
    silently dropped download/split/stitch progress on every reopen. Returns
    {pdf, gemini_parts, strip, video_url}, each null if that step hasn't happened yet.
    `strip` is the same preview payload /editor_state/load returns, so the UI
    can show an already-stitched strip without re-stitching it."""
    chapter_id = sanitize_chapter(request.args.get("chapter", ""))
    if not chapter_id:
        return jsonify(error="chapter required"), 400

    pdf_path = downloaded_pdf(chapter_id)
    pdf = ({"path": str(pdf_path), "name": pdf_path.name,
            "page_count": pdf_page_count(pdf_path)}
           if pdf_path is not None else None)

    parts = existing_gemini_parts(chapter_id)
    gemini_parts = {"dir": str(gemini_parts_dir(chapter_id)), "parts": parts} if parts else None

    slug, ch_no = scope_params()
    video_url = _video_url(chapter_id, chapter_output_dir(chapter_id) / "recap.mp4", slug, ch_no)
    return jsonify(pdf=pdf, gemini_parts=gemini_parts, strip=preview_payload(chapter_id),
                   video_url=video_url)

"""Routes that serve a chapter's output files and open folders."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from flask import Blueprint, jsonify, request, send_from_directory

from config import OUTPUT_DIR, PROJECTS_DIR, WORK_DIR, chapter_output_dir
from services.common import is_under, sanitize_chapter

bp = Blueprint("files", __name__)


@bp.get("/output/<chapter_id>/<path:name>")
def output_file(chapter_id: str, name: str):
    """Serve a finished artifact (e.g. recap.mp4) for in-page play/download. Flask's
    send_from_directory honours Range requests, so the <video> element scrubs."""
    out_dir = chapter_output_dir(sanitize_chapter(chapter_id))
    if not (out_dir / name).exists():
        return jsonify(error=f"{name} not found for this chapter."), 404
    return send_from_directory(str(out_dir), name)


@bp.post("/open_folder")
def open_folder():
    """Open a folder in the OS file browser (Explorer on Windows). Body: JSON
    {path}. Restricted to paths under work/ so this can't be used to open
    arbitrary locations on disk."""
    data = request.get_json(silent=True) or {}
    raw = (data.get("path") or "").strip().strip('"')
    if not raw:
        return jsonify(error="path required"), 400
    p = Path(raw).resolve()
    allowed_roots = (WORK_DIR.resolve(), OUTPUT_DIR.resolve(), PROJECTS_DIR.resolve())
    if not any(is_under(p, root) for root in allowed_roots):
        return jsonify(error="Refusing to open a path outside work/, output/, or projects/."), 400
    if not p.is_dir():
        return jsonify(error=f"No such folder: {p}"), 404
    try:
        if sys.platform == "win32":
            os.startfile(str(p))  # noqa: S606 - path is validated above
        elif sys.platform == "darwin":
            subprocess.run(["open", str(p)], check=False)
        else:
            subprocess.run(["xdg-open", str(p)], check=False)
    except Exception as e:  # noqa: BLE001 - report, don't crash
        return jsonify(error=f"{type(e).__name__}: {e}"), 500
    return jsonify(ok=True)

"""Routes for a chapter's source: download, stitch, tiles, crops, script loading and export."""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

from PIL import Image
from flask import Blueprint, jsonify, request, send_file, send_from_directory

from services.chapter_files import THUMB_MAX, _framer_dir, _tiles_dir, ensure_dir, stitch_chapter
from services.common import Refused, sanitize_chapter, scope_params
from services.downloads import _download_chapter
from services.frames import DEFAULT_BLUR_STYLE, blur_region, export_chapter
from services.script_text import split_script_into_lines

bp = Blueprint("chapters", __name__)


@bp.post("/download")
def download():
    """Download ONE chapter as a PDF.

    Body: JSON {url, chapter_no?, chapter_id?}. Returns {chapter_id, pdf, page_count,
    steps:[...]} - `pdf` is the full chapter PDF that feeds Stitch PDF."""
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(_download_chapter((data.get("url") or "").strip().strip('"'),
                                         str(data.get("chapter_no") or "").strip(),
                                         (data.get("chapter_id") or "").strip(), *scope_params()))
    except Refused as e:
        return jsonify(error=str(e)), e.status


@bp.post("/load_pdf")
def load_pdf():
    """Stitch a PDF (path in JSON, or an uploaded file) into one strip.

    Body: JSON {"pdf_path": "...", "chapter_id": "..."}  OR  multipart with a
    "pdf" file part (+ optional "chapter_id" form field).
    """
    upload = request.files.get("pdf")
    if upload is not None and upload.filename:
        chapter_id = sanitize_chapter(
            request.form.get("chapter_id") or Path(upload.filename).stem
        )
        pdf_path = ensure_dir(_framer_dir(chapter_id)) / "_upload.pdf"
        upload.save(str(pdf_path))
    else:
        data = request.get_json(silent=True) or {}
        raw = (data.get("pdf_path") or "").strip().strip('"')
        if not raw:
            return jsonify(error="Provide a pdf_path or upload a PDF file."), 400
        pdf_path = Path(raw)
        if not pdf_path.is_file():
            return jsonify(error=f"PDF not found: {pdf_path}"), 400
        chapter_id = sanitize_chapter(data.get("chapter_id") or pdf_path.stem)

    try:
        payload = stitch_chapter(pdf_path, chapter_id)
    except Exception as e:  # noqa: BLE001 - report stitch failures to the UI
        return jsonify(error=f"Stitch failed: {type(e).__name__}: {e}"), 500
    return jsonify(payload)


@bp.get("/tiles/<chapter_id>/<name>")
def tile(chapter_id: str, name: str):
    """Serve one preview tile PNG (display only)."""
    if not re.fullmatch(r"tile_\d{4}\.png", name):
        return jsonify(error="bad tile name"), 400
    tiles_dir = _tiles_dir(sanitize_chapter(chapter_id))
    if not (tiles_dir / name).exists():
        return jsonify(error="No such tile; load a PDF first."), 404
    return send_from_directory(str(tiles_dir), name, mimetype="image/png")


@bp.get("/crop")
def crop():
    """Return a small thumbnail for a box, cropped from the PREVIEW tiles.

    Query: chapter, x, y, w, h  (in TRUE strip px). Cropping from the already-
    downscaled tiles keeps this light (no loading the full 600k-px strip into
    memory per thumbnail); thumbnails only need preview resolution anyway. The
    horizontal flip is applied in CSS on the client, so it's ignored here.
    """
    chapter_id = sanitize_chapter(request.args.get("chapter", ""))
    meta_path = _framer_dir(chapter_id) / "preview.json"
    if not meta_path.exists():
        return jsonify(error="No preview; load a PDF first."), 404
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    ps, pw, ph, tile_h = meta["preview_scale"], meta["preview_w"], meta["preview_h"], meta["tile_h"]

    try:
        tx, ty, tw, th = (float(request.args[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, ValueError):
        return jsonify(error="x,y,w,h required"), 400

    # Optional blur regions (TRUE strip px) so the thumbnail / strip overlay shows
    # the blur LIVE — the same effect the exporter bakes in. Format:
    #   blur=x,y,w,h[,style];x,y,w,h[,style];...   (style: "m" mosaic, "g" gaussian)
    regions = []
    for chunk in request.args.get("blur", "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(",")
        try:
            nums = [float(v) for v in parts[:4]]
        except ValueError:
            continue
        if len(nums) < 4:
            continue
        style = parts[4] if len(parts) > 4 else DEFAULT_BLUR_STYLE
        regions.append((nums[0], nums[1], nums[2], nums[3], style))

    # true px -> preview px, clamped into the preview canvas.
    px = max(0, min(int(round(tx * ps)), pw - 1))
    py = max(0, min(int(round(ty * ps)), ph - 1))
    bw = max(1, min(int(round(tw * ps)), pw - px))
    bh = max(1, min(int(round(th * ps)), ph - py))

    result = Image.new("RGB", (bw, bh), (20, 20, 20))
    tiles_dir = _tiles_dir(chapter_id)
    i0, i1 = py // tile_h, (py + bh - 1) // tile_h
    for i in range(i0, i1 + 1):
        tpath = tiles_dir / f"tile_{i:04d}.png"
        if not tpath.exists():
            continue
        with Image.open(tpath) as timg:
            ty0 = i * tile_h
            top = max(py, ty0)
            bot = min(py + bh, ty0 + timg.height)
            if bot <= top:
                continue
            patch = timg.crop((px, top - ty0, px + bw, bot - ty0)).convert("RGB")
        result.paste(patch, (0, top - py))

    # Apply the SAME strengthened effect the exporter uses to each region. Working
    # in preview px keeps it size-proportional, so the preview matches the export.
    for rx, ry, rw, rh, style in regions:
        lx = int(round(rx * ps)) - px
        ly = int(round(ry * ps)) - py
        rxx = lx + max(1, int(round(rw * ps)))
        ryy = ly + max(1, int(round(rh * ps)))
        lx, ly = max(0, lx), max(0, ly)
        rxx, ryy = min(bw, rxx), min(bh, ryy)
        if rxx <= lx or ryy <= ly:
            continue  # region doesn't overlap this crop
        blur_region(result, lx, ly, rxx, ryy, style)

    k = min(THUMB_MAX / bw, THUMB_MAX / bh, 4.0)
    thumb = result.resize((max(1, round(bw * k)), max(1, round(bh * k))), Image.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@bp.post("/load_script")
def load_script():
    """Split pasted text OR a .txt file into ordered narration lines.

    Body: JSON {"text": "..."}  OR  {"txt_path": "..."}. Auto-splits prose
    into short segments (see split_script_into_lines) - it does NOT assume
    one segment per input line, and blank/whitespace-only lines never
    produce empty segments.
    """
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    if not text:
        raw = (data.get("txt_path") or "").strip().strip('"')
        if raw:
            p = Path(raw)
            if not p.is_file():
                return jsonify(error=f"Text file not found: {p}"), 400
            text = p.read_text(encoding="utf-8", errors="replace")
    if not text:
        return jsonify(error="Provide text or a txt_path."), 400

    lines = split_script_into_lines(text)
    return jsonify(lines=[text for text, _ in lines], paces=[pace for _, pace in lines])


@bp.post("/export")
def export():
    """Render every frame + write mapping.json AND a pipeline manifest.

    Body: JSON {"chapter_id": "...", "entries": [ {line_text, duration_s, pace,
    mode:"sequence"|"together", frames:[ {bbox:[x,y,w,h], flip:bool,
    blur:[[x,y,w,h],...]}, ... ]}, ... ]} with all coords in TRUE strip pixels and
    entries already in final (narration) order. A line may hold MULTIPLE frames.

    Per line we crop/flip/blur every frame from the full-res strip into
    frame_<NNN><letter>.png (letter preserves intra-line order), and emit one
    mapping.json line: {order, text, duration_s, pace, mode, frames:[{file, image,
    flip, blur_regions, bbox}]}.

    The pipeline manifest (consumed by s6 audio + s7 video) gets ONE beat per
    LINE so audio stays one-narration-per-line. The beat's visible image is the
    line's FIRST frame - enough for the current s7 to render a valid baseline
    video. To honour sequence/together, s7 reads mapping.json (see README:
    "What s7 must change").
    """
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(export_chapter(sanitize_chapter(data.get("chapter_id") or ""),
                                       data.get("entries") or [], *scope_params()))
    except Refused as e:
        return jsonify(error=str(e)), e.status

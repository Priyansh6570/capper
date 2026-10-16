"""A chapter's files on disk: the stitched strip and its preview tiles, and the downloaded PDF."""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

from PIL import Image

from atomic_io import atomic_write_json
from config import chapter_work_dir
from manifest import Manifest
from stages import s1_pdf_to_pages as s1


# Preview is for DISPLAY only - crops always come from the full-res strip.
#
# A webtoon strip is narrow but VERY tall (e.g. 1667 x 600000). One scale that
# fits both width and height into a single image would crush the width to a few
# px. So we scale by WIDTH only (to PREVIEW_W) for real detail, and TILE the
# resulting tall preview vertically - no single image/canvas blows past browser
# limits, and tiles load lazily. The view layer adds zoom + scroll on top.
PREVIEW_W = 1000          # downscaled preview width in px (detail to zoom into)


TILE_PREVIEW_H = 4000     # height of each preview tile in px (browser-safe)


THUMB_MAX = 220           # max px for a line's crop thumbnail


def _framer_dir(chapter_id: str) -> Path:
    """The framer's folder for the chapter. Not created here: writers create it (see ensure_dir)."""
    return chapter_work_dir(chapter_id) / "framer"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _strip_path(chapter_id: str) -> Path:
    return chapter_work_dir(chapter_id) / "pages" / "strip_001.png"


def _tiles_dir(chapter_id: str) -> Path:
    return _framer_dir(chapter_id) / "tiles"


def make_preview(strip: Image.Image, chapter_id: str) -> dict:
    """Downscale the strip to PREVIEW_W wide, sliced into vertical tiles.

    `preview_scale` maps TRUE -> preview ( preview_px = true_px * preview_scale ),
    so the frontend recovers full-res coords with true_px = preview_px /
    preview_scale (then the view layer multiplies by the zoom factor for display
    only). Each tile is cropped from the full-res strip and resized, so memory
    stays bounded even for a 600k-px strip. Geometry is persisted to preview.json
    so the thumbnail-crop endpoint works after a restart.
    """
    sw, sh = strip.width, strip.height
    ps = min(1.0, PREVIEW_W / sw)
    pw, ph = max(1, round(sw * ps)), max(1, round(sh * ps))

    tiles_dir = _tiles_dir(chapter_id)
    shutil.rmtree(tiles_dir, ignore_errors=True)
    tiles_dir.mkdir(parents=True, exist_ok=True)

    n = max(1, math.ceil(ph / TILE_PREVIEW_H))
    tiles = []
    for i in range(n):
        y0 = i * TILE_PREVIEW_H
        y1 = min(ph, y0 + TILE_PREVIEW_H)
        th = y1 - y0
        ty0, ty1 = round(y0 / ps), round(y1 / ps)         # true-px band
        band = strip.crop((0, ty0, sw, min(sh, ty1))).resize((pw, th), Image.LANCZOS)
        name = f"tile_{i:04d}.png"
        band.save(str(tiles_dir / name))
        tiles.append({"url": f"/tiles/{chapter_id}/{name}", "y": y0, "w": pw, "h": th})

    atomic_write_json(
        _framer_dir(chapter_id) / "preview.json",
        {"preview_scale": ps, "preview_w": pw, "preview_h": ph,
         "strip_w": sw, "strip_h": sh, "tile_h": TILE_PREVIEW_H, "n": n},
    )
    return {
        "chapter_id": chapter_id,
        "strip_w": sw, "strip_h": sh,
        "preview_w": pw, "preview_h": ph,
        "preview_scale": ps,
        "tile_h": TILE_PREVIEW_H,
        "tiles": tiles,
    }


def stitch_pdf(pdf_path: Path, chapter_id: str) -> Image.Image:
    """Stitch a PDF into one strip by RUNNING stage 1's webtoon path.

    We hand stage 1 a throwaway Manifest pointing at the PDF with doc_type
    "webtoon"; it renders every page and vertically concatenates them into
    work/<chapter>/pages/strip_001.png exactly as the pipeline does.
    """
    m = Manifest(
        chapter_id=chapter_id,
        source_pdf=str(pdf_path),
        work_dir=str(chapter_work_dir(chapter_id)),
        doc_type="webtoon",
    )
    s1.run(m)  # writes strip_001.png (+ a stage-1 preview we ignore)
    return Image.open(_strip_path(chapter_id))


# --------------------------------------------------------------------------- #
# download a chapter (site adapter, see tools/framer/sites/) + split for Gemini
# --------------------------------------------------------------------------- #
def download_dir(chapter_id: str) -> Path:
    d = chapter_work_dir(chapter_id) / "download"
    d.mkdir(parents=True, exist_ok=True)
    return d


def downloaded_pdf(chapter_id: str) -> Path | None:
    """The chapter's already-downloaded PDF (see /download), if any - most
    recently written, since an adapter may nest it under a subfolder."""
    pdfs = sorted(download_dir(chapter_id).rglob("*.pdf"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return pdfs[0] if pdfs else None


def pdf_page_count(pdf_path: Path) -> int:
    import fitz  # PyMuPDF (already a project dep; imported lazily)
    doc = fitz.open(str(pdf_path))
    try:
        return doc.page_count
    finally:
        doc.close()


def preview_payload(chapter_id: str) -> dict | None:
    """Rebuild the /load_pdf preview payload for an ALREADY-stitched chapter.

    Used by /editor_state/load so a saved session resumes WITHOUT re-stitching the PDF:
    reads geometry from preview.json and re-derives the tile list. If the preview
    or its tiles are missing but the strip is on disk, it is regenerated; if there
    is no strip either, returns None (caller restores lines only).
    """
    meta_path = _framer_dir(chapter_id) / "preview.json"
    tiles_dir = _tiles_dir(chapter_id)
    have_tiles = tiles_dir.exists() and any(tiles_dir.glob("tile_*.png"))
    meta = None
    if meta_path.exists() and have_tiles:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = None  # corrupt preview.json - fall through to regenerating it below
    if meta is None:
        # preview.json is a disposable cache (re-derivable from the strip), but
        # this whole function gates /editor_state/load - a corrupt/missing
        # cache here must never block getting back to the actual boxing/script
        # session, so it's regenerated instead of raising.
        strip_path = _strip_path(chapter_id)
        if not strip_path.exists():
            return None
        return make_preview(Image.open(strip_path).convert("RGB"), chapter_id)

    ps, pw, ph = meta["preview_scale"], meta["preview_w"], meta["preview_h"]
    tile_h, n = meta["tile_h"], meta["n"]
    sw = meta.get("strip_w") or round(pw / ps)
    sh = meta.get("strip_h") or round(ph / ps)
    tiles = []
    for i in range(n):
        y0 = i * tile_h
        y1 = min(ph, y0 + tile_h)
        tiles.append({"url": f"/tiles/{chapter_id}/tile_{i:04d}.png",
                      "y": y0, "w": pw, "h": y1 - y0})
    return {
        "chapter_id": chapter_id, "strip_w": sw, "strip_h": sh,
        "preview_w": pw, "preview_h": ph, "preview_scale": ps,
        "tile_h": tile_h, "tiles": tiles,
    }


def stitch_chapter(pdf_path: Path, chapter_id: str) -> dict:
    """Stitch a PDF into the chapter's one strip; returns the preview payload."""
    payload = make_preview(stitch_pdf(pdf_path, chapter_id), chapter_id)
    payload["source_pdf"] = str(pdf_path)
    return payload

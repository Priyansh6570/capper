"""A chapter's files on disk: the stitched strip and its preview tiles, the downloaded PDF and the
Gemini-ready parts split from it."""
from __future__ import annotations

import io
import json
import math
import re
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


# Gemini-part prep: a downloaded chapter PDF is ALWAYS split into <=PART_PAGES-page
# parts (Google AI Studio / Gemini caps pages per uploaded PDF well above this, but
# smaller parts upload faster and keep each request well inside size limits too).
# Consecutive parts OVERLAP by a few pages so Gemini has continuity across the seam
# when reading two parts back-to-back. Each part's pages are downscaled to
# GEMINI_MAX_DIM px (longest side) and re-encoded as JPEG at GEMINI_JPEG_QUALITY
# inside the PDF, trading a little sharpness for a much smaller upload.
PART_PAGES = 100            # max pages per Gemini part


PART_OVERLAP = 5            # pages repeated at the start of each part after the first


GEMINI_MAX_DIM = 1600       # downscale target, px (longest side)


GEMINI_JPEG_QUALITY = 75    # JPEG quality for the re-encoded page images


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


def gemini_parts_dir(chapter_id: str) -> Path:
    d = chapter_work_dir(chapter_id) / "gemini_parts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def existing_gemini_parts(chapter_id: str) -> list[dict]:
    """Parts already on disk for `chapter_id`, in the same shape
    /gemini_parts_stream's `done` event uses (see build_gemini_parts_stream)."""
    parts = []
    for p in sorted(gemini_parts_dir(chapter_id).glob("*.pdf")):
        m = re.search(r"_p(\d+)-(\d+)\.pdf$", p.name)
        a, b = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        parts.append({"path": str(p), "name": p.name, "pages": max(0, b - a + 1),
                      "from": a, "to": b, "size_bytes": p.stat().st_size})
    return parts


def _fmt_size(n: int) -> str:
    """Human-readable file size (e.g. 3.4MB)."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"  # pragma: no cover - unreachable, satisfies linters


def _gemini_part_ranges(total: int, part_pages: int, overlap: int) -> list[tuple[int, int]]:
    """0-based [start, end) page ranges covering `total` pages, each <=`part_pages`
    long, where every part after the first repeats `overlap` pages from the tail of
    the previous one (for cross-seam continuity). A chapter within `part_pages`
    yields a single, non-overlapping part."""
    part_pages = max(1, part_pages)
    overlap = max(0, min(overlap, part_pages - 1))
    step = part_pages - overlap
    ranges: list[tuple[int, int]] = []
    start = 0
    while True:
        end = min(start + part_pages, total)
        ranges.append((start, end))
        if end >= total:
            break
        start += step
    return ranges


def _compress_page_jpeg(fitz_mod, page, max_dim: int, quality: int) -> tuple[int, int, bytes]:
    """Render one PDF page, downscale so its longest side is <=`max_dim`, and
    re-encode as JPEG at `quality`. Returns (width, height, jpeg_bytes)."""
    rect = page.rect
    longest = max(rect.width, rect.height) or 1.0
    zoom = max(0.05, max_dim / longest)
    pix = page.get_pixmap(matrix=fitz_mod.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if max(img.width, img.height) > max_dim:
        scale = max_dim / max(img.width, img.height)
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                          Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return img.width, img.height, buf.getvalue()


def build_gemini_parts_stream(pdf_path: Path, chapter_id: str,
                                part_pages: int = PART_PAGES,
                                overlap: int = PART_OVERLAP,
                                max_dim: int = GEMINI_MAX_DIM,
                                quality: int = GEMINI_JPEG_QUALITY):
    """Generator: split `pdf_path` into <=`part_pages`-page parts (consecutive parts
    OVERLAP by `overlap` pages), downscaling + JPEG-recompressing every page so each
    part stays small but legible. ALWAYS runs, even for a chapter within
    `part_pages` (it still becomes one compressed part). Parts land in
    work/<chapter>/gemini_parts/, cleared first so re-downloading is idempotent.

    Yields progress dicts as it works; the LAST item is always
    {"type": "result", "parts": [...], "dir": str} with each part
    {path, name, pages, from, to, size_bytes} (1-based inclusive page numbers).
    This is the ONLY thing that reads/writes gemini_parts/ - it never touches the
    full-res PDF or the stitched strip used by Stitch/Framer.
    """
    import fitz

    doc = fitz.open(str(pdf_path))
    try:
        total = doc.page_count
        ranges = _gemini_part_ranges(total, part_pages, overlap)
        out_dir = gemini_parts_dir(chapter_id)
        for old in out_dir.glob("*.pdf"):
            old.unlink()

        width = len(str(len(ranges)))
        parts: list[dict] = []
        for idx, (a, b) in enumerate(ranges):
            part_no = idx + 1
            yield {"type": "log",
                   "line": f"Part {part_no}/{len(ranges)}: pages {a + 1}-{b} "
                           f"({b - a}p) - downscaling to {max_dim}px + JPEG "
                           f"q{quality}…"}
            out_pdf = fitz.open()
            for i, pno in enumerate(range(a, b)):
                w, h, jpeg_bytes = _compress_page_jpeg(fitz, doc.load_page(pno), max_dim, quality)
                out_page = out_pdf.new_page(width=w, height=h)
                out_page.insert_image(out_page.rect, stream=jpeg_bytes)
                if (i + 1) % 20 == 0 or (i + 1) == (b - a):
                    yield {"type": "progress", "part": part_no, "of_parts": len(ranges),
                           "page": i + 1, "of_pages": b - a}
            name = f"{pdf_path.stem}_part{part_no:0{width}d}_p{a + 1}-{b}.pdf"
            out_path = out_dir / name
            out_pdf.save(str(out_path), garbage=4, deflate=True)
            out_pdf.close()
            size_bytes = out_path.stat().st_size
            parts.append({"path": str(out_path), "name": name, "pages": b - a,
                          "from": a + 1, "to": b, "size_bytes": size_bytes})
            yield {"type": "log",
                   "line": f"  -> {name}  ({b - a}p, {_fmt_size(size_bytes)})"}
        yield {"type": "result", "parts": parts, "dir": str(out_dir)}
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

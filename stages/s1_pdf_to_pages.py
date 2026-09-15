"""Stage 1 - source pages to page images.  [REAL]  Runs on: your laptop (CPU).

This is the entry point and the only fully implemented stage; everything
downstream starts as a stub you replace.

Two SOURCE kinds, auto-detected from disk:
  - IMAGE FOLDER (work/<chapter>/input/*.{png,jpg,...}): a chapter pre-downloaded
    as separate webtoon slices (e.g. 229 images from webtoon-downloader). Loaded
    in natural filename order and stitched exactly like the PDF webtoon path. An
    image-folder chapter is inherently a vertical strip, so it is always treated
    as a webtoon regardless of m.doc_type. Detected by the presence of images in
    work/<chapter>/input/; no PDF is needed.
  - PDF (m.source_pdf): rendered with PyMuPDF, branching on m.doc_type:
      - "manga":   one Page per PDF page (page-based layout).
      - "webtoon": render every page, then vertically stitch them IN ORDER into
                   one continuous strip. PDF page breaks in a webtoon export are
                   arbitrary slices of a single tall image, so we glue them back.

For both webtoon-style paths the manifest gets a single Page (index 1) pointing
at work/<chapter>/pages/strip_001.png (+ a small strip_001_preview.png).
"""
import re

import fitz  # PyMuPDF
from PIL import Image

from config import PAGE_DPI, chapter_work_dir
from manifest import Manifest, Page

# Tall webtoon strips blow past Pillow's default decompression-bomb guard.
# These are trusted local renders, so lift the ceiling. (229 stacked slices make
# a very tall image.)
Image.MAX_IMAGE_PIXELS = None

# Preview strip height — small enough to open quickly for a visual sanity check.
PREVIEW_HEIGHT = 1500

# Edge-trim safeguard: treat a row as trimmable only if every pixel matches the
# corner color within this per-channel tolerance.
_TRIM_TOLERANCE = 6

# Image-folder source: extensions we ingest, and where they live.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def chapter_input_dir(chapter_id: str):
    """Folder a chapter's pre-downloaded image slices live in (if any)."""
    return chapter_work_dir(chapter_id) / "input"


def list_input_images(chapter_id: str):
    """Image slices for a chapter, in natural filename order; [] if none.

    Natural order so panel_2 sorts before panel_10 (not lexicographic). The
    orchestrator uses this to detect an image-folder chapter.
    """
    input_dir = chapter_input_dir(chapter_id)
    if not input_dir.is_dir():
        return []
    files = [p for p in input_dir.iterdir()
             if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    return sorted(files, key=_natural_key)


def _natural_key(path):
    return [int(tok) if tok.isdigit() else tok.lower()
            for tok in re.split(r"(\d+)", path.name)]


def run(m: Manifest) -> Manifest:
    images = list_input_images(m.chapter_id)
    if images:
        return _run_image_folder(m, images)
    if m.doc_type == "webtoon":
        return _run_webtoon(m)
    return _run_manga(m)


def _run_manga(m: Manifest) -> Manifest:
    pages_dir = chapter_work_dir(m.chapter_id) / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(m.source_pdf)
    zoom = PAGE_DPI / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    m.pages = []
    for i, page in enumerate(doc, start=1):
        pix = page.get_pixmap(matrix=matrix)
        out = pages_dir / f"p{i:03d}.png"
        pix.save(str(out))
        m.pages.append(Page(index=i, image=str(out)))
    doc.close()

    print(f"  rendered {len(m.pages)} pages -> {pages_dir}")
    m.stage = "pages"
    return m


def _run_webtoon(m: Manifest) -> Manifest:
    pages_dir = chapter_work_dir(m.chapter_id) / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(m.source_pdf)
    zoom = PAGE_DPI / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    # Render each PDF page to an in-order list of PIL images.
    slices = []
    for i, page in enumerate(doc, start=1):
        pix = page.get_pixmap(matrix=matrix)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        slices.append(img)
    doc.close()

    if not slices:
        raise SystemExit(f"No pages rendered from {m.source_pdf}")

    return _stitch_and_save(m, slices, f"stitched {len(slices)} pages")


def _run_image_folder(m: Manifest, image_paths) -> Manifest:
    """Stitch a folder of pre-downloaded webtoon slices into one tall strip.

    Same stitching as the PDF webtoon path; the only difference is the source —
    standalone image files instead of rendered PDF pages.
    """
    print(f"  loading {len(image_paths)} image(s) from {chapter_input_dir(m.chapter_id)}")

    slices = []
    for p in image_paths:
        with Image.open(p) as img:
            slices.append(img.convert("RGB"))

    if not slices:
        raise SystemExit(f"No images found in {chapter_input_dir(m.chapter_id)}")

    return _stitch_and_save(m, slices, f"stitched {len(slices)} images")


def _stitch_and_save(m: Manifest, slices, what: str) -> Manifest:
    """Vertically concatenate in-order RGB slices into one strip + preview.

    Shared by the PDF-webtoon and image-folder paths. Writes a single Page
    (index 1) pointing at the strip.
    """
    pages_dir = chapter_work_dir(m.chapter_id) / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    # Align every slice to the widest one (left-aligned, pad right with white).
    max_w = max(s.width for s in slices)
    aligned = []
    for s in slices:
        if s.width == max_w:
            aligned.append(s)
        else:
            canvas = Image.new("RGB", (max_w, s.height), (255, 255, 255))
            canvas.paste(s, (0, 0))
            aligned.append(canvas)

    total_h = sum(s.height for s in aligned)
    strip = Image.new("RGB", (max_w, total_h), (255, 255, 255))
    y = 0
    for s in aligned:
        strip.paste(s, (0, y))
        y += s.height

    # Light safeguard trim of uniform solid-color top/bottom EDGE rows. Exports
    # here have no margins, so this is usually a no-op.
    strip = _trim_solid_edges(strip)

    strip_path = pages_dir / "strip_001.png"
    strip.save(str(strip_path))

    preview = strip
    if strip.height > PREVIEW_HEIGHT:
        scale = PREVIEW_HEIGHT / strip.height
        preview = strip.resize(
            (max(1, round(strip.width * scale)), PREVIEW_HEIGHT), Image.LANCZOS
        )
    preview_path = pages_dir / "strip_001_preview.png"
    preview.save(str(preview_path))

    m.pages = [Page(index=1, image=str(strip_path))]

    print(f"  {what} -> strip {strip.width}x{strip.height} -> {strip_path}")
    m.stage = "pages"
    return m


def _trim_solid_edges(img: Image.Image) -> Image.Image:
    """Crop away top/bottom edge rows that are a single uniform color.

    Only trims contiguous solid rows at the very top and bottom; stops at the
    first row that varies. A no-op for margin-less exports.
    """
    w, h = img.size
    px = img.load()

    def row_is_solid(y: int) -> bool:
        r0, g0, b0 = px[0, y]
        for x in range(w):
            r, g, b = px[x, y]
            if (abs(r - r0) > _TRIM_TOLERANCE
                    or abs(g - g0) > _TRIM_TOLERANCE
                    or abs(b - b0) > _TRIM_TOLERANCE):
                return False
        return True

    top = 0
    while top < h and row_is_solid(top):
        top += 1
    bottom = h
    while bottom > top and row_is_solid(bottom - 1):
        bottom -= 1

    if top == 0 and bottom == h:
        return img
    return img.crop((0, top, w, bottom))

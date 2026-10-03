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
import struct
import zlib

import fitz  # PyMuPDF
import numpy as np
from PIL import Image

from config import PAGE_DPI, chapter_work_dir
from manifest import Manifest, Page

# Tall webtoon strips blow past Pillow's default decompression-bomb guard.
# These are trusted local renders, so lift the ceiling. (229 stacked slices make
# a very tall image.)
Image.MAX_IMAGE_PIXELS = None

# Preview strip height — small enough to open quickly for a visual sanity check.
PREVIEW_HEIGHT = 1500

# Rows encoded per PNG band when streaming the strip to disk.
_BAND_ROWS = 256

_PNG_SIGNATURE = bytes([137, 80, 78, 71, 13, 10, 26, 10])

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
    doc = fitz.open(m.source_pdf)
    try:
        if not doc.page_count:
            raise SystemExit(f"No pages rendered from {m.source_pdf}")
        matrix = fitz.Matrix(PAGE_DPI / 72.0, PAGE_DPI / 72.0)
        sizes = []
        for page in doc:
            box = (page.rect * matrix).irect
            sizes.append((box.width, box.height))

        def load(i: int) -> Image.Image:
            pix = doc[i].get_pixmap(matrix=matrix)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

        return _stitch_and_save(m, sizes, load, f"stitched {len(sizes)} pages")
    finally:
        doc.close()


def _run_image_folder(m: Manifest, image_paths) -> Manifest:
    """Stitch a folder of pre-downloaded webtoon slices into one tall strip.

    Same stitching as the PDF webtoon path; the only difference is the source -
    standalone image files instead of rendered PDF pages.
    """
    print(f"  loading {len(image_paths)} image(s) from {chapter_input_dir(m.chapter_id)}")
    if not image_paths:
        raise SystemExit(f"No images found in {chapter_input_dir(m.chapter_id)}")

    sizes = []
    for p in image_paths:
        with Image.open(p) as img:
            sizes.append(img.size)

    def load(i: int) -> Image.Image:
        with Image.open(image_paths[i]) as img:
            return img.convert("RGB")

    return _stitch_and_save(m, sizes, load, f"stitched {len(sizes)} images")


def _stitch_and_save(m: Manifest, sizes, load, what: str) -> Manifest:
    """Vertically concatenate in-order slices into one strip + preview.

    `sizes` is each slice's (width, height); `load(i)` returns slice i as an RGB
    image. Slices are decoded one at a time and the PNG is encoded band by band,
    so peak memory is one slice, not the whole strip. Shared by the PDF-webtoon
    and image-folder paths. Writes a single Page (index 1) pointing at the strip.
    """
    pages_dir = chapter_work_dir(m.chapter_id) / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    width = max(w for w, _ in sizes)
    total_h = sum(h for _, h in sizes)
    top, bottom = _edge_trim(sizes, load, width)
    height = total_h - top - bottom
    if height <= 0:
        raise SystemExit("Stitched strip is a single solid color")

    scale = min(1.0, PREVIEW_HEIGHT / height)
    preview = Image.new(
        "RGB", (max(1, round(width * scale)), max(1, round(height * scale))), (255, 255, 255)
    )

    def bands():
        y = 0
        for kept in _kept_slices(sizes, load, width, top, total_h - bottom):
            _paste_preview(preview, kept, y, scale)
            for r in range(0, len(kept), _BAND_ROWS):
                yield kept[r:r + _BAND_ROWS]
            y += len(kept)

    strip_path = pages_dir / "strip_001.png"
    _write_png(strip_path, width, height, bands())
    preview.save(str(pages_dir / "strip_001_preview.png"))

    m.pages = [Page(index=1, image=str(strip_path))]

    print(f"  {what} -> strip {width}x{height} -> {strip_path}")
    m.stage = "pages"
    return m


def _padded(img: Image.Image, width: int) -> np.ndarray:
    """Slice as an (h, width, 3) array, right-padded with white to `width`."""
    arr = np.asarray(img)
    if arr.shape[1] == width:
        return arr
    canvas = np.full((arr.shape[0], width, 3), 255, np.uint8)
    canvas[:, :arr.shape[1]] = arr
    return canvas


def _kept_slices(sizes, load, width: int, start: int, stop: int):
    """Yield each slice's rows that fall inside strip rows [start, stop)."""
    y = 0
    for i, (_, h) in enumerate(sizes):
        lo, hi = max(start - y, 0), min(h, stop - y)
        if lo < hi:
            yield _padded(load(i), width)[lo:hi]
        y += h


def _edge_trim(sizes, load, width: int) -> tuple[int, int]:
    """Count solid-color rows at the strip's top and bottom.

    Light safeguard; exports here have no margins, so this is usually a no-op
    that only decodes the first and last slice.
    """
    def solid_run(order, flip: bool) -> int:
        count = 0
        for i in order:
            solid = _solid_rows(_padded(load(i), width))
            if flip:
                solid = solid[::-1]
            run = len(solid) if solid.all() else int(np.argmin(solid))
            count += run
            if run < len(solid):
                break
        return count

    n = len(sizes)
    return solid_run(range(n), False), solid_run(reversed(range(n)), True)


def _solid_rows(arr: np.ndarray) -> np.ndarray:
    """Per row: True if every pixel matches the row's first pixel within tolerance."""
    diff = np.abs(arr.astype(np.int16) - arr[:, :1])
    return diff.max(axis=(1, 2)) <= _TRIM_TOLERANCE


def _paste_preview(preview: Image.Image, rows: np.ndarray, y: int, scale: float) -> None:
    """Downscale `rows` (strip rows starting at y) into their slot of the preview."""
    top = round(y * scale)
    th = round((y + len(rows)) * scale) - top
    if th <= 0:
        return
    band = Image.fromarray(rows)
    if scale < 1.0:
        band = band.resize((preview.width, th), Image.LANCZOS)
    preview.paste(band, (0, top))


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data)))


def _write_png(path, width: int, height: int, bands) -> None:
    """Stream-encode an 8-bit RGB PNG from an iterable of (n, width, 3) row bands.

    PNG's zlib stream is sequential, so rows can be compressed and written as
    they arrive. The strip can be 100M+ px and is only read back locally, so
    compress level 1 trades file size for much faster saves.
    """
    comp = zlib.compressobj(1)
    with open(path, "wb") as f:
        f.write(_PNG_SIGNATURE)
        f.write(_png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        for band in bands:
            raw = np.zeros((len(band), 1 + width * 3), np.uint8)  # filter type 0 per row
            raw[:, 1:] = band.reshape(len(band), -1)
            data = comp.compress(raw.tobytes())
            if data:
                f.write(_png_chunk(b"IDAT", data))
        f.write(_png_chunk(b"IDAT", comp.flush()))
        f.write(_png_chunk(b"IEND", b""))

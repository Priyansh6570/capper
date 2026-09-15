"""Stage 2 - LOCAL candidate panel cutter for vertical webtoon strips.  [REAL]
Runs on: your laptop (CPU). No Kaggle, no GPU, no network.

Stage 1 stitches a chapter's raw downloaded slices into ONE tall clean strip
(`work/<chapter>/pages/strip_001.png`). This stage cuts that strip into
horizontal CANDIDATE panels and writes one crop per candidate, top-to-bottom =
reading order. A later selection stage trims the candidates, so we deliberately
OVER-cut: better too many than too few.

How it cuts (no ML):
  1. Gutter detection - scan rows of the strip for near-uniform horizontal bands
     (low per-row pixel variance: blank/solid gutters between art) and cut at the
     middle of each long gutter run, so breaks land in whitespace, not mid-art.
  2. Min-height floor - a candidate shorter than MIN_PANEL_H is a sliver; it
     merges upward into the panel above it (a leading sliver merges down).
  3. Max-height fallback - a candidate taller than MAX_PANEL_H (a long gutterless
     stretch) is split into readable chunks, cutting at the most-uniform rows
     near each even boundary so even forced cuts avoid slicing through art.

OCR is best-effort and NON-FATAL: each candidate's text is read (if an OCR engine
is installed) and stored, deduped, in `Panel.dialogue` (the field stage 5 reads).
A crop with no text is expected and fine. If no OCR engine is available we warn
once and leave dialogue empty - cutting still succeeds.

Owns: Page.panels (bbox, order, crop, dialogue). Leaves context/emotion (s3),
script_line (s5) empty. run(m) -> m, sets m.stage = "panels", never saves.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Allow running this file directly (python stages/s2_detect_panels.py) for the
# self-test; harmless when imported as part of the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import chapter_work_dir
from manifest import Manifest, Panel

# Tall webtoon strips blow past Pillow's default decompression-bomb guard.
Image.MAX_IMAGE_PIXELS = None

# --------------------------------------------------------------------------- #
# Tunables - adjust cut aggressiveness here.
# --------------------------------------------------------------------------- #
# A row counts as "gutter" (blank/solid background) when its per-row grayscale
# standard deviation is below this. Raise it to treat busier rows as gutters
# (more cuts); lower it to cut only at truly flat whitespace (fewer cuts).
GUTTER_ROW_STD = 8.0

# A run of at least this many consecutive gutter rows is a real scene break we
# cut at. Lower => more cuts (over-cut harder); higher => only wide gutters.
MIN_GUTTER_PX = 20

# Candidates shorter than this (px) are slivers and merge into the neighbour
# above (a leading sliver merges into the one below).
MIN_PANEL_H = 200

# Candidates taller than this (px) are force-split into chunks of ~this height.
MAX_PANEL_H = 2200

# Row analysis is done on a width-downsampled copy (keeps memory sane on a strip
# that can be hundreds of thousands of px tall). Height is untouched, so row
# indices map 1:1 back to full-res Y. Cosmetic only; doesn't change which rows
# are gutters in practice.
ANALYSIS_WIDTH = 400

# Skip OCR on crops smaller than this on either side (nothing to read).
OCR_MIN_SIDE = 12

# Debug overlay (cut lines + reading-order numbers) - the easiest way to catch
# bad cuts. Downscaled so it opens quickly.
WRITE_DEBUG = True
DEBUG_PREVIEW_MAX_H = 4000


# --------------------------------------------------------------------------- #
# Row uniformity / gutter analysis
# --------------------------------------------------------------------------- #
def _row_std(gray: np.ndarray) -> np.ndarray:
    """Per-row standard deviation, computed in row-blocks to bound memory.

    `gray` is a (H, W) uint8 array. Returns a length-H float32 array. Done in
    blocks because a full-strip float upcast would be gigabytes on a tall strip.
    """
    h = gray.shape[0]
    out = np.empty(h, dtype=np.float32)
    block = 8192
    for y0 in range(0, h, block):
        y1 = min(h, y0 + block)
        chunk = gray[y0:y1].astype(np.float32)
        out[y0:y1] = chunk.std(axis=1)
    return out


def _gutter_cuts(row_std: np.ndarray) -> list[int]:
    """Cut at the midpoint of every run of >= MIN_GUTTER_PX gutter rows."""
    h = row_std.shape[0]
    is_gutter = row_std < GUTTER_ROW_STD
    cuts: list[int] = []
    y = 0
    while y < h:
        if is_gutter[y]:
            start = y
            while y < h and is_gutter[y]:
                y += 1
            if y - start >= MIN_GUTTER_PX:
                cuts.append((start + y) // 2)
        else:
            y += 1
    return cuts


def _spans_from_cuts(cuts: list[int], height: int) -> list[tuple[int, int]]:
    bounds = [0] + cuts + [height]
    return [(bounds[i], bounds[i + 1])
            for i in range(len(bounds) - 1)
            if bounds[i + 1] - bounds[i] > 1]


def _merge_slivers(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Fold candidates shorter than MIN_PANEL_H into their neighbour above."""
    merged: list[tuple[int, int]] = []
    for top, bottom in spans:
        if merged and (bottom - top) < MIN_PANEL_H:
            merged[-1] = (merged[-1][0], bottom)        # absorb upward
        else:
            merged.append((top, bottom))
    # A leading sliver had no neighbour above; merge it into the one below.
    if len(merged) >= 2 and (merged[0][1] - merged[0][0]) < MIN_PANEL_H:
        first = merged.pop(0)
        merged[0] = (first[0], merged[0][1])
    return merged


def _split_oversized(spans: list[tuple[int, int]],
                     row_std: np.ndarray) -> list[tuple[int, int]]:
    """Split any candidate taller than MAX_PANEL_H into even-ish chunks.

    Each forced cut is nudged to the most-uniform (lowest-std) row within a
    window of the even target, so even a gutterless stretch breaks at the
    flattest available row instead of straight through art.
    """
    out: list[tuple[int, int]] = []
    for top, bottom in spans:
        height = bottom - top
        if height <= MAX_PANEL_H:
            out.append((top, bottom))
            continue
        n = math.ceil(height / MAX_PANEL_H)
        radius = max(1, int(0.15 * height / n))
        prev = top
        for k in range(1, n):
            target = top + round(k * height / n)
            cut = _min_std_row(row_std, target, radius)
            cut = max(prev + 1, min(cut, bottom - 1))
            out.append((prev, cut))
            prev = cut
        out.append((prev, bottom))
    return out


def _min_std_row(row_std: np.ndarray, center: int, radius: int) -> int:
    lo = max(0, center - radius)
    hi = min(row_std.shape[0], center + radius + 1)
    if hi <= lo:
        return center
    return lo + int(np.argmin(row_std[lo:hi]))


def cut_candidates(gray: np.ndarray) -> list[tuple[int, int]]:
    """Full cut pipeline: gutters -> merge slivers -> split oversized."""
    row_std = _row_std(gray)
    spans = _spans_from_cuts(_gutter_cuts(row_std), gray.shape[0])
    if not spans:                                   # no gutters at all: whole strip
        spans = [(0, gray.shape[0])]
    spans = _merge_slivers(spans)
    spans = _split_oversized(spans, row_std)
    return spans


# --------------------------------------------------------------------------- #
# OCR - pluggable + best-effort. Tries installed engines in order; returns a
# read(img)->str callable, or None if no engine is available (non-fatal).
# --------------------------------------------------------------------------- #
def _load_ocr():
    """Return (engine_name, read_fn) or (None, None) if no OCR is installed."""
    # 1. pytesseract (needs the Tesseract binary on PATH).
    try:
        import pytesseract
        pytesseract.get_tesseract_version()          # raises if binary missing

        def read(img: Image.Image) -> str:
            return pytesseract.image_to_string(img)

        return "pytesseract", read
    except Exception:
        pass

    # 2. easyocr (CPU).
    try:
        import easyocr
        reader = easyocr.Reader(["en"], gpu=False, verbose=False)

        def read(img: Image.Image) -> str:
            return " ".join(reader.readtext(np.asarray(img.convert("RGB")),
                                            detail=0))

        return "easyocr", read
    except Exception:
        pass

    # 3. paddleocr (CPU) - same engine the old Kaggle stage used.
    try:
        from paddleocr import PaddleOCR
        engine = PaddleOCR(use_angle_cls=True, lang="en", show_log=False,
                           use_gpu=False)

        def read(img: Image.Image) -> str:
            result = engine.ocr(np.asarray(img.convert("RGB")), cls=True)
            if not result or not result[0]:
                return ""
            return " ".join(line[1][0] for line in result[0]
                            if line and line[1] and line[1][0])

        return "paddleocr", read
    except Exception:
        pass

    return None, None


def _clean_ocr(text: str) -> str | None:
    """Collapse OCR whitespace and drop consecutive duplicate lines."""
    if not text:
        return None
    lines: list[str] = []
    for raw in text.splitlines():
        seg = " ".join(raw.split())
        if seg and (not lines or _norm(seg) != _norm(lines[-1])):
            lines.append(seg)
    joined = " ".join(lines).strip()
    return joined or None


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


# --------------------------------------------------------------------------- #
# Debug overlay
# --------------------------------------------------------------------------- #
def _write_debug(page_img: Image.Image, spans, debug_path: Path,
                 start_order: int) -> None:
    canvas = page_img.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)
    w = canvas.width
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 48)
    except OSError:
        font = ImageFont.load_default()
    for i, (top, bottom) in enumerate(spans):
        draw.line([(0, top), (w, top)], fill=(255, 0, 0), width=4)
        draw.rectangle([6, top + 6, 6 + 110, top + 6 + 60], fill=(255, 0, 0))
        draw.text((16, top + 10), str(start_order + i), fill=(255, 255, 255),
                  font=font)
    if canvas.height > DEBUG_PREVIEW_MAX_H:
        scale = DEBUG_PREVIEW_MAX_H / canvas.height
        canvas = canvas.resize(
            (max(1, round(canvas.width * scale)), DEBUG_PREVIEW_MAX_H),
            Image.LANCZOS,
        )
    canvas.save(debug_path)


# --------------------------------------------------------------------------- #
# Stage entry point
# --------------------------------------------------------------------------- #
def run(m: Manifest) -> Manifest:
    work = chapter_work_dir(m.chapter_id)
    panels_dir = work / "panels"
    panels_dir.mkdir(parents=True, exist_ok=True)

    if not m.pages:
        raise SystemExit(
            "Stage 2: no pages in manifest. Run stage 1 first to produce the "
            "stitched strip."
        )

    ocr_name, ocr_read = _load_ocr()
    if ocr_name:
        print(f"  OCR engine: {ocr_name}")
    else:
        print("  OCR unavailable (no pytesseract/easyocr/paddleocr); "
              "leaving dialogue empty - cutting still proceeds")

    order = 0
    ocr_filled = 0
    for page in m.pages:
        page.panels = []                            # replace any stub panels

        strip_path = Path(page.image)
        if not strip_path.is_file():
            raise SystemExit(
                f"Stage 2: page {page.index} image not found: {strip_path}. "
                f"Re-run stage 1."
            )
        strip = Image.open(strip_path)
        full_w, full_h = strip.size

        # Width-downsampled grayscale for row analysis (height preserved).
        gray_img = strip.convert("L")
        if full_w > ANALYSIS_WIDTH:
            gray_img = gray_img.resize((ANALYSIS_WIDTH, full_h), Image.BILINEAR)
        gray = np.asarray(gray_img, dtype=np.uint8)

        spans = cut_candidates(gray)

        rgb = strip.convert("RGB")
        page_start_order = order + 1
        for top, bottom in spans:
            order += 1
            crop = rgb.crop((0, top, full_w, bottom))
            crop_name = f"panel_{order:03d}.png"
            crop_path = panels_dir / crop_name
            crop.save(crop_path)

            dialogue = None
            if ocr_read is not None and crop.width >= OCR_MIN_SIDE \
                    and crop.height >= OCR_MIN_SIDE:
                try:
                    dialogue = _clean_ocr(ocr_read(crop))
                except Exception as e:              # noqa: BLE001 - OCR is non-fatal
                    print(f"    [warn] OCR failed on {crop_name} "
                          f"({type(e).__name__}); leaving text empty")
                if dialogue:
                    ocr_filled += 1

            page.panels.append(
                Panel(
                    id=f"p{page.index:03d}_o{order:04d}",
                    bbox=[0, int(top), int(full_w), int(bottom - top)],
                    order=order,
                    crop=str(crop_path),
                    dialogue=dialogue,
                )
            )

        if WRITE_DEBUG:
            debug_path = panels_dir / f"_debug_p{page.index:03d}.png"
            _write_debug(rgb, spans, debug_path, page_start_order)

    ocr_note = (f", OCR text on {ocr_filled}/{order}" if ocr_name
                else ", no OCR")
    print(f"  cut {order} candidate panels -> {panels_dir}{ocr_note}")
    m.stage = "panels"
    return m


# --------------------------------------------------------------------------- #
# Self-test: build a synthetic strip (content bands + white gutters + one
# over-tall band) and assert the cutter behaves. Run: python stages/s2_detect_panels.py
# --------------------------------------------------------------------------- #
def _self_test() -> None:
    import tempfile

    import config
    from manifest import Manifest, Page

    rng = np.random.default_rng(0)
    width = 300
    gutter = 60
    # Three normal bands + one over-tall band (must be force-split).
    bands = [600, 600, 600, MAX_PANEL_H + 800]

    parts = []
    for i, bh in enumerate(bands):
        content = rng.integers(0, 256, size=(bh, width, 3), dtype=np.uint8)
        parts.append(content)
        if i != len(bands) - 1:
            parts.append(np.full((gutter, width, 3), 255, dtype=np.uint8))
    arr = np.concatenate(parts, axis=0)
    strip = Image.fromarray(arr)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        orig_work = config.WORK_DIR
        config.WORK_DIR = tmp
        try:
            chapter = "ch_test"
            work = tmp / chapter
            (work / "pages").mkdir(parents=True, exist_ok=True)
            strip_path = work / "pages" / "strip_001.png"
            strip.save(strip_path)

            m = Manifest(
                chapter_id=chapter,
                source_pdf="",
                work_dir=str(work),
                doc_type="webtoon",
                pages=[Page(index=1, image=str(strip_path),
                            panels=[Panel(id="stub", bbox=[0, 0, 0, 0], order=1)])],
            )
            m = run(m)

            assert m.stage == "panels"
            panels = m.pages[0].panels
            # 3 normal bands + the tall band split into >= 2 => at least 5.
            assert len(panels) >= 5, len(panels)

            heights = [p.bbox[3] for p in panels]
            # Over-tall band was split: nothing exceeds the max.
            assert max(heights) <= MAX_PANEL_H, max(heights)
            # No surviving slivers (single-panel edge case excluded).
            assert min(heights) >= MIN_PANEL_H, min(heights)

            # Reading order is 1..N, contiguous, top-to-bottom.
            assert [p.order for p in panels] == list(range(1, len(panels) + 1))
            tops = [p.bbox[1] for p in panels]
            assert tops == sorted(tops)

            for p in panels:
                assert Path(p.crop).is_file(), p.crop
                assert Path(p.crop).name.startswith("panel_")
                # s2 owns crop/bbox/order/dialogue only; later stages fill these.
                assert p.context is None and p.script_line is None

            assert (work / "panels" / "_debug_p001.png").is_file()

            # Unit checks on the cut helpers.
            assert _spans_from_cuts([10], 100) == [(0, 10), (10, 100)]
            assert _merge_slivers([(0, 50), (50, 800)]) == [(0, 800)]
            assert _clean_ocr("") is None
            assert _clean_ocr("hi\nhi\nbye") == "hi bye"
            print(f"s2 self-test OK ({len(panels)} candidates)")
        finally:
            config.WORK_DIR = orig_work


if __name__ == "__main__":
    _self_test()

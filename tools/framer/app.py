"""Framer - browser-based manual frame-selection tool.

A small Flask app that lets you hand-bind each narration line of a recap script
to a frame you draw on the chapter strip, then export a mapping the EXISTING
pipeline turns into audio + video.

What it does:
  * /load_pdf   - stitch ALL PDF pages into one tall strip using the SAME code as
                  stages/s1_pdf_to_pages.py (we import and run stage 1's webtoon
                  path - PyMuPDF render + vertical concat + edge trim, with
                  Pillow's decompression-bomb guard lifted). Returns a downscaled
                  preview (canvas-safe) plus the scale factor so the browser can
                  map preview pixels back to TRUE strip pixels.
  * /load_script- split pasted text / a .txt file into ordered narration lines.
  * /export     - crop each hand-drawn bbox from the FULL-RES strip, horizontal-
                  flip + gaussian-blur as requested, and write:
                      work/<chapter>/frames/frame_NNN.png   (the rendered frames)
                      work/<chapter>/framer/mapping.json     (line/frame/duration)
                      work/<chapter>/manifest.json           (pipeline manifest)
                  The manifest's `beats` are populated and `stage` is set to
                  "clean" so the orchestrator runs ONLY audio + video next:
                      python orchestrator.py --chapter <chapter> --from audio

Run:  python tools/framer/app.py        (prints the localhost URL)

Self-contained under tools/framer/. The only reused project code is stage 1's
stitch (imported, not reimplemented) and the Manifest schema for the export.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

# --- make the project root importable so we can REUSE stage 1 + the manifest --
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, Response, jsonify, request, send_file, send_from_directory  # noqa: E402
from PIL import Image, ImageFilter  # noqa: E402

# Stage 1: importing it lifts Pillow's MAX_IMAGE_PIXELS guard (module-level) and
# gives us the exact stitch used by the real pipeline.
from stages import s1_pdf_to_pages as s1  # noqa: E402
import config  # noqa: E402 - module import so chapter_scope()/set_/clear_ are reachable
from config import chapter_work_dir, chapter_output_dir, WORK_DIR, OUTPUT_DIR, PROJECTS_DIR  # noqa: E402
from manifest import Manifest, Page, Panel  # noqa: E402

import projects  # noqa: E402 - the project system (this package: tools/framer/projects.py)
import webtoon_meta  # noqa: E402 - best-effort series-metadata scraper
from stages import s7_assemble as s7  # noqa: E402 - reused by the settings live-preview routes

# Belt-and-suspenders: ensure the bomb guard stays off even if import order shifts.
Image.MAX_IMAGE_PIXELS = None

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
# Blur for HIDING text. The effect strength scales with the blurred region's
# smaller side (not a fixed radius), so even big speech bubbles are destroyed.
# The SAME numbers drive the live preview and the exported frame (WYSIWYG).
BLUR_STRENGTH = 0.30      # gaussian radius / mosaic block size = this * minside
DEFAULT_BLUR_STYLE = "m"  # "m" = pixelate/mosaic (most unreadable), "g" = heavy gaussian

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
WEBTOON_TIMEOUT_S = 1800  # hard cap on a single webtoon-downloader run (30 min)

HERE = Path(__file__).resolve().parent
app = Flask(__name__)

# Session-wide UI settings, kept SERVER-SIDE in memory (NOT browser localStorage)
# so a theme / layout choice survives page reloads while the app is running.
_SETTINGS = {"theme": "dark", "split": None}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# project scoping - every route that touches chapter files redirects
# chapter_work_dir()/chapter_output_dir() (see config.py) to
# projects/<slug>/chapters/<n>/{work,output} for the duration of the request,
# by reading `project` + `ch_no` (query args, JSON body, or form field - never
# collides with the pre-existing `chapter`/`chapter_id` label param).
#
# NOTE on the two SSE routes (/generate_stream, /gemini_parts_stream): Flask
# tears the request context down before a streamed generator's body actually
# runs, so before_request's scope would be gone by the time chapter_work_dir()
# is called inside those generators. Those two routes therefore compute their
# own (work_dir, output_dir) up front and wrap their generator body in an
# explicit `with config.chapter_scope(...)` - see their route functions below.
# --------------------------------------------------------------------------- #
def _scope_params() -> tuple[str | None, str | None]:
    data = request.get_json(silent=True) if request.is_json else None
    data = data or {}
    form = request.form if request.form else {}
    slug = request.args.get("project") or data.get("project") or form.get("project")
    ch_no = request.args.get("ch_no") or data.get("ch_no") or form.get("ch_no")
    return (slug or None), (str(ch_no) if ch_no else None)


@app.before_request
def _apply_chapter_scope():
    slug, ch_no = _scope_params()
    if slug and ch_no:
        config.set_chapter_scope(projects.chapter_work_dir(slug, ch_no),
                                  projects.chapter_output_dir(slug, ch_no))
    else:
        config.clear_chapter_scope()


@app.teardown_request
def _clear_chapter_scope(exc=None):
    config.clear_chapter_scope()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _is_under(path: Path, root: Path) -> bool:
    """True if `path` (already resolved) sits inside `root` (already resolved).
    normcase handles Windows' case-insensitive / drive-letter-cased paths."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return os.path.normcase(str(path)).startswith(os.path.normcase(str(root)))


def _sanitize_chapter(name: str) -> str:
    """A safe chapter_id from arbitrary text (used as a folder name)."""
    name = (name or "").strip()
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return name or "ch_framer"


def _framer_dir(chapter_id: str) -> Path:
    d = chapter_work_dir(chapter_id) / "framer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _strip_path(chapter_id: str) -> Path:
    return chapter_work_dir(chapter_id) / "pages" / "strip_001.png"


def _tiles_dir(chapter_id: str) -> Path:
    return _framer_dir(chapter_id) / "tiles"


def _make_preview(strip: Image.Image, chapter_id: str) -> dict:
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

    (_framer_dir(chapter_id) / "preview.json").write_text(
        json.dumps({"preview_scale": ps, "preview_w": pw, "preview_h": ph,
                    "strip_w": sw, "strip_h": sh, "tile_h": TILE_PREVIEW_H, "n": n}),
        encoding="utf-8",
    )
    return {
        "chapter_id": chapter_id,
        "strip_w": sw, "strip_h": sh,
        "preview_w": pw, "preview_h": ph,
        "preview_scale": ps,
        "tile_h": TILE_PREVIEW_H,
        "tiles": tiles,
    }


def _stitch_pdf(pdf_path: Path, chapter_id: str) -> Image.Image:
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
    strip = Image.open(_strip_path(chapter_id)).convert("RGB")
    return strip


# --------------------------------------------------------------------------- #
# download a chapter (webtoon-downloader) + split for Gemini
# --------------------------------------------------------------------------- #
def _download_dir(chapter_id: str) -> Path:
    d = chapter_work_dir(chapter_id) / "download"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _webtoon_cmd() -> list[str] | None:
    """Locate the webtoon-downloader CLI: the installed console script if on PATH,
    else `python -m webtoon_downloader` when only the package is importable.
    Returns the command prefix, or None if the tool isn't installed."""
    exe = shutil.which("webtoon-downloader")
    if exe:
        return [exe]
    try:
        import importlib.util
        if importlib.util.find_spec("webtoon_downloader") is not None:
            return [sys.executable, "-m", "webtoon_downloader"]
    except Exception:  # noqa: BLE001 - any import probe failure -> treat as absent
        pass
    return None


_WEBTOON_OUT_FLAG: str | None = None


def _webtoon_out_flag(base: list[str]) -> str:
    """The output-directory flag for the installed webtoon-downloader: newer builds
    use `--out` (older `--dest` is rejected/deprecated). Probed once from --help and
    cached; defaults to `--out`."""
    global _WEBTOON_OUT_FLAG
    if _WEBTOON_OUT_FLAG is None:
        _WEBTOON_OUT_FLAG = "--out"
        try:
            h = subprocess.run(base + ["--help"], capture_output=True, text=True,
                               timeout=30)
            text = (h.stdout or "") + (h.stderr or "")
            if "--out" not in text and "--dest" in text:
                _WEBTOON_OUT_FLAG = "--dest"
        except Exception:  # noqa: BLE001 - probe failure -> keep the modern default
            pass
    return _WEBTOON_OUT_FLAG


def _run_webtoon_downloader(url: str, chapter_no: str, dest: Path
                            ) -> tuple[list[str], subprocess.CompletedProcess]:
    """Fetch ONE chapter to `dest` as a PDF with webtoon-downloader. When a chapter
    number is given we pin --start/--end to it so exactly that chapter is pulled."""
    base = _webtoon_cmd()
    if base is None:
        raise RuntimeError(
            "webtoon-downloader is not installed. Install it with:\n"
            "    pip install webtoon-downloader")
    cmd = base + [url, _webtoon_out_flag(base), str(dest), "--save-as", "pdf"]
    if chapter_no:
        cmd += ["--start", str(chapter_no), "--end", str(chapter_no)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=WEBTOON_TIMEOUT_S)
    return cmd, proc


def _newest_pdf(dest: Path) -> Path | None:
    """The most recently written PDF anywhere under `dest` (webtoon-downloader may
    nest it under a series/chapter subfolder)."""
    pdfs = sorted(dest.rglob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    return pdfs[0] if pdfs else None


def _pdf_page_count(pdf_path: Path) -> int:
    import fitz  # PyMuPDF (already a project dep; imported lazily)
    doc = fitz.open(str(pdf_path))
    try:
        return doc.page_count
    finally:
        doc.close()


def _gemini_parts_dir(chapter_id: str) -> Path:
    d = chapter_work_dir(chapter_id) / "gemini_parts"
    d.mkdir(parents=True, exist_ok=True)
    return d


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


def _build_gemini_parts_stream(pdf_path: Path, chapter_id: str,
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
        out_dir = _gemini_parts_dir(chapter_id)
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


def _preview_payload(chapter_id: str) -> dict | None:
    """Rebuild the /load_pdf preview payload for an ALREADY-stitched chapter.

    Used by /editor_state/load so a saved session resumes WITHOUT re-stitching the PDF:
    reads geometry from preview.json and re-derives the tile list. If the preview
    or its tiles are missing but the strip is on disk, it is regenerated; if there
    is no strip either, returns None (caller restores lines only).
    """
    meta_path = _framer_dir(chapter_id) / "preview.json"
    tiles_dir = _tiles_dir(chapter_id)
    have_tiles = tiles_dir.exists() and any(tiles_dir.glob("tile_*.png"))
    if not meta_path.exists() or not have_tiles:
        strip_path = _strip_path(chapter_id)
        if not strip_path.exists():
            return None
        return _make_preview(Image.open(strip_path).convert("RGB"), chapter_id)

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
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


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.get("/assets/<path:name>")
def app_asset(name):
    """Serves the app's own branding assets (logo/icon) from ROOT/assets -
    used by index.html for the favicon and header logo."""
    return send_from_directory(str(ROOT / "assets"), name)


@app.post("/download")
def download():
    """Download ONE webtoon.com chapter as a PDF. Gemini-part splitting/compression
    is a SEPARATE step (see /gemini_parts_stream) so its progress can stream.

    Body: JSON {url, chapter_no?, chapter_id?}. Runs webtoon-downloader
    (--save-as pdf) into work/<chapter>/download/. Returns {chapter_id, pdf,
    page_count, steps:[...]} - `pdf` is the full chapter PDF that feeds BOTH
    Stitch PDF (unchanged) and Gemini-part prep."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip().strip('"')
    raw_id = (data.get("chapter_id") or "").strip()
    chapter_no = str(data.get("chapter_no") or "").strip()

    if not url:
        return jsonify(error="Paste a webtoon.com chapter URL."), 400
    if "webtoon" not in url.lower():
        return jsonify(error="That doesn't look like a webtoon.com URL."), 400
    if not raw_id and not chapter_no:
        return jsonify(error="Enter the chapter number (or a chapter id)."), 400
    chapter_id = _sanitize_chapter(raw_id or chapter_no)

    dest = _download_dir(chapter_id)
    steps = [f"Downloading chapter {chapter_no or '(from URL)'} into {dest} …"]
    try:
        cmd, proc = _run_webtoon_downloader(url, chapter_no, dest)
    except RuntimeError as e:                 # tool not installed
        return jsonify(error=str(e)), 500
    except subprocess.TimeoutExpired:
        return jsonify(error=f"webtoon-downloader timed out after "
                             f"{WEBTOON_TIMEOUT_S // 60} min."), 504

    steps.append("$ " + " ".join(cmd))
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()
        tail = tail[-1000:] if tail else "(no output)"
        return jsonify(error=f"webtoon-downloader exited {proc.returncode}:\n{tail}"), 500

    pdf = _newest_pdf(dest)
    if pdf is None:
        return jsonify(error=f"Download finished but no PDF was found under {dest}. "
                             f"Check that this webtoon-downloader build supports "
                             f"--save-as pdf."), 500

    pages = _pdf_page_count(pdf)
    steps.append(f"Downloaded {pdf.name} — {pages} page(s):\n    {pdf}")

    slug, ch_no = _scope_params()
    if slug and ch_no:
        projects.set_status(slug, ch_no, "downloaded")

    return jsonify(
        chapter_id=chapter_id,
        pdf=str(pdf),
        page_count=pages,
        steps=steps,
    )


@app.get("/gemini_parts_stream")
def gemini_parts_stream():
    """Split + compress a chapter PDF into Gemini-ready parts, streaming progress.

    EventSource (GET) only, params in the query string:
        ?pdf_path=...&chapter_id=...&project=<slug>&ch_no=<n>
    `project`+`ch_no` scope the run to the project chapter's work dir (see the
    note above _scope_params - this generator re-enters the scope explicitly
    since the request context is gone by the time it actually runs).
    Always splits into <=PART_PAGES-page parts (PART_OVERLAP pages of overlap
    between consecutive parts) and downscales/re-JPEGs every page (see
    _build_gemini_parts_stream). Emits {type:start|log|progress|done|error}; `done`
    carries {parts:[{path,name,pages,from,to,size_bytes}], dir}."""
    raw = (request.args.get("pdf_path") or "").strip().strip('"')
    chapter_id = _sanitize_chapter(request.args.get("chapter_id", ""))
    pdf_path = Path(raw)
    slug, ch_no = _scope_params()
    work_dir = projects.chapter_work_dir(slug, ch_no) if slug and ch_no else chapter_work_dir(chapter_id)
    output_dir = projects.chapter_output_dir(slug, ch_no) if slug and ch_no else chapter_output_dir(chapter_id)

    def stream():
        with config.chapter_scope(work_dir, output_dir):
            if not raw or not pdf_path.is_file():
                yield _sse({"type": "error", "message": f"PDF not found: {pdf_path}"})
                return
            try:
                total = _pdf_page_count(pdf_path)
            except Exception as e:  # noqa: BLE001 - report, don't crash the stream
                yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
                return

            yield _sse({"type": "start", "total_pages": total,
                        "message": f"Preparing Gemini parts for {pdf_path.name} "
                                   f"({total} pages) — ≤{PART_PAGES}p/part, "
                                   f"{PART_OVERLAP}p overlap, max {GEMINI_MAX_DIM}px, "
                                   f"JPEG q{GEMINI_JPEG_QUALITY}…"})
            try:
                for item in _build_gemini_parts_stream(pdf_path, chapter_id):
                    if item["type"] == "result":
                        yield _sse({"type": "done", "ok": True,
                                    "parts": item["parts"], "dir": item["dir"]})
                    else:
                        yield _sse(item)
            except Exception as e:  # noqa: BLE001 - report, don't crash the stream
                yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@app.post("/load_pdf")
def load_pdf():
    """Stitch a PDF (path in JSON, or an uploaded file) into one strip.

    Body: JSON {"pdf_path": "...", "chapter_id": "..."}  OR  multipart with a
    "pdf" file part (+ optional "chapter_id" form field).
    """
    upload = request.files.get("pdf")
    if upload is not None and upload.filename:
        chapter_id = _sanitize_chapter(
            request.form.get("chapter_id") or Path(upload.filename).stem
        )
        pdf_path = _framer_dir(chapter_id) / "_upload.pdf"
        upload.save(str(pdf_path))
    else:
        data = request.get_json(silent=True) or {}
        raw = (data.get("pdf_path") or "").strip().strip('"')
        if not raw:
            return jsonify(error="Provide a pdf_path or upload a PDF file."), 400
        pdf_path = Path(raw)
        if not pdf_path.is_file():
            return jsonify(error=f"PDF not found: {pdf_path}"), 400
        chapter_id = _sanitize_chapter(data.get("chapter_id") or pdf_path.stem)

    try:
        strip = _stitch_pdf(pdf_path, chapter_id)
    except Exception as e:  # noqa: BLE001 - report stitch failures to the UI
        return jsonify(error=f"Stitch failed: {type(e).__name__}: {e}"), 500

    payload = _make_preview(strip, chapter_id)
    payload["source_pdf"] = str(pdf_path)
    return jsonify(payload)


@app.get("/tiles/<chapter_id>/<name>")
def tile(chapter_id: str, name: str):
    """Serve one preview tile PNG (display only)."""
    if not re.fullmatch(r"tile_\d{4}\.png", name):
        return jsonify(error="bad tile name"), 400
    tiles_dir = _tiles_dir(_sanitize_chapter(chapter_id))
    if not (tiles_dir / name).exists():
        return jsonify(error="No such tile; load a PDF first."), 404
    return send_from_directory(str(tiles_dir), name, mimetype="image/png")


@app.get("/crop")
def crop():
    """Return a small thumbnail for a box, cropped from the PREVIEW tiles.

    Query: chapter, x, y, w, h  (in TRUE strip px). Cropping from the already-
    downscaled tiles keeps this light (no loading the full 600k-px strip into
    memory per thumbnail); thumbnails only need preview resolution anyway. The
    horizontal flip is applied in CSS on the client, so it's ignored here.
    """
    chapter_id = _sanitize_chapter(request.args.get("chapter", ""))
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
        _blur_region(result, lx, ly, rxx, ryy, style)

    k = min(THUMB_MAX / bw, THUMB_MAX / bh, 4.0)
    thumb = result.resize((max(1, round(bw * k)), max(1, round(bh * k))), Image.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")



# --------------------------------------------------------------------------- #
# script paste -> segments. NOT a line-per-line split: pasted narration is
# usually prose (one or more paragraphs, possibly hard-wrapped by whatever
# editor it was written in), and a whole paragraph makes an unusably long,
# unboxable segment. So this treats blank-line-separated PARAGRAPHS as the
# input unit, splits each at SENTENCE boundaries, then greedily repacks
# consecutive sentences into segments sized for roughly SCRIPT_TARGET_SEC of
# narration each (stage 6's real TTS clip length is what actually drives
# on-screen timing - this is only a same-ballpark starting point so segments
# come out boxable without manual re-splitting).
# --------------------------------------------------------------------------- #
_SCRIPT_CHARS_PER_SEC = 15          # rough narration pace: ~150wpm * ~6 chars/word / 60s
_SCRIPT_TARGET_SEC = 4.0
_SCRIPT_TARGET_CHARS = round(_SCRIPT_CHARS_PER_SEC * _SCRIPT_TARGET_SEC)   # ~60
_SCRIPT_MAX_CHARS = round(_SCRIPT_TARGET_CHARS * 1.5)                      # ~90 - hard cap
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')
_CLAUSE_SPLIT_RE = re.compile(r'(?<=[,;:])\s+')
_WS_RE = re.compile(r'\s+')


def _normalize_ws(s: str) -> str:
    # Collapses ANY whitespace run - including non-breaking spaces pasted
    # from word processors, which .strip() alone leaves behind, turning a
    # visually-blank line into a "non-empty" one that survives filtering.
    return _WS_RE.sub(' ', s).strip()


def _split_long_sentence(sentence: str) -> list[str]:
    """A sentence over the hard cap: break at clause punctuation first,
    falling back to a plain word-boundary wrap, so no one segment is
    absurdly long (run-on sentences, list-like prose, etc.)."""
    parts = [p for p in _CLAUSE_SPLIT_RE.split(sentence) if p]
    out = []
    for part in parts:
        while len(part) > _SCRIPT_MAX_CHARS:
            cut = part.rfind(' ', 0, _SCRIPT_MAX_CHARS)
            if cut <= 0:
                cut = _SCRIPT_MAX_CHARS
            out.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            out.append(part)
    return out or [sentence]


def split_script_into_lines(text: str) -> list[str]:
    """Turn pasted narration prose into short, boxable segments (see module
    note above). Paragraph order and sentence order are both preserved."""
    lines: list[str] = []
    for para in re.split(r'\n\s*\n', text):
        blob = _normalize_ws(para)
        if not blob:
            continue
        sentences = [s for s in (x.strip() for x in _SENTENCE_SPLIT_RE.split(blob)) if s]
        cur = ""
        for sent in sentences:
            pieces = [sent] if len(sent) <= _SCRIPT_MAX_CHARS else _split_long_sentence(sent)
            for piece in pieces:
                if not cur:
                    cur = piece
                elif len(cur) >= _SCRIPT_TARGET_CHARS or len(cur) + 1 + len(piece) > _SCRIPT_MAX_CHARS:
                    lines.append(cur)
                    cur = piece
                else:
                    cur = cur + " " + piece
            if len(pieces) > 1:
                # A sentence long enough to need clause-splitting: never let
                # its trailing clause fragment blend into the NEXT sentence's
                # segment (e.g. a short "Finally," clause gluing onto the
                # sentence before it) - flush now, keep fragments confined
                # to the sentence they came from.
                lines.append(cur)
                cur = ""
        if cur:
            lines.append(cur)
    return lines


@app.post("/load_script")
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
    return jsonify(lines=lines)


def _frame_suffix(j: int) -> str:
    """Letter suffix for the j-th (0-based) frame of a line: a, b, ... z, aa, ...

    Keeps per-line frame ORDER lexicographically sortable in the filename
    (frame_001a.png < frame_001b.png), so reading order survives on disk.
    """
    s = ""
    while True:
        s = chr(ord("a") + j % 26) + s
        j = j // 26 - 1
        if j < 0:
            return s


def _region_box_style(region):
    """A blur region is [x,y,w,h] or [x,y,w,h,style]; return (x,y,w,h,style)."""
    x, y, w, h = (int(round(float(v))) for v in region[:4])
    style = region[4] if len(region) > 4 else DEFAULT_BLUR_STYLE
    return x, y, w, h, style


def _blur_region(img: Image.Image, lx: int, ly: int, rx: int, ry: int, style: str) -> None:
    """Destroy text in img[lx:rx, ly:ry] IN PLACE, intensity scaled to the region
    size so big bubbles are hit as hard as small ones.

    style "g" = heavy gaussian (radius = BLUR_STRENGTH * minside); anything else =
    mosaic/pixelate (downscale then nearest-neighbour upscale), which reduces the
    region to a few blocks and is the most reliable way to make text unreadable.
    Shared by BOTH the export and the live preview so they look identical.
    """
    w, h = rx - lx, ry - ly
    if w <= 0 or h <= 0:
        return
    minside = min(w, h)
    patch = img.crop((lx, ly, rx, ry))
    if style == "g":
        patch = patch.filter(ImageFilter.GaussianBlur(max(8.0, BLUR_STRENGTH * minside)))
    else:  # mosaic / pixelate (default)
        block = max(3, int(round(BLUR_STRENGTH * minside)))
        sw, sh = max(1, w // block), max(1, h // block)
        patch = patch.resize((sw, sh), Image.BILINEAR).resize((w, h), Image.NEAREST)
    img.paste(patch, (lx, ly))


def _crop_frame(strip: Image.Image, entry: dict, out_path: Path) -> None:
    """Crop one frame from the full-res strip, then flip + blur as requested.

    `entry` carries this frame's bbox + blur regions in TRUE strip pixels and a
    flip flag. Blur regions are translated into crop-local coords and gaussian-
    blurred in place; the flip is applied last so on-screen left/right matches
    what the user toggled.
    """
    x, y, w, h = (int(round(v)) for v in entry["bbox"])
    x = max(0, min(x, strip.width - 1))
    y = max(0, min(y, strip.height - 1))
    w = max(1, min(w, strip.width - x))
    h = max(1, min(h, strip.height - y))
    crop = strip.crop((x, y, x + w, y + h))

    for region in entry.get("blur") or []:
        bx, by, bw, bh, style = _region_box_style(region)
        # strip coords -> crop-local coords
        lx, ly = bx - x, by - y
        rx2, ry2 = lx + bw, ly + bh
        lx, ly = max(0, lx), max(0, ly)
        rx2, ry2 = min(crop.width, rx2), min(crop.height, ry2)
        if rx2 <= lx or ry2 <= ly:
            continue  # blur region doesn't overlap this crop
        _blur_region(crop, lx, ly, rx2, ry2, style)

    if entry.get("flip"):
        crop = crop.transpose(Image.FLIP_LEFT_RIGHT)

    crop.save(str(out_path))


@app.post("/export")
def export():
    """Render every frame + write mapping.json AND a pipeline manifest.

    Body: JSON {"chapter_id": "...", "entries": [ {line_text, duration_s,
    mode:"sequence"|"together", frames:[ {bbox:[x,y,w,h], flip:bool,
    blur:[[x,y,w,h],...]}, ... ]}, ... ]} with all coords in TRUE strip pixels and
    entries already in final (narration) order. A line may hold MULTIPLE frames.

    Per line we crop/flip/blur every frame from the full-res strip into
    frame_<NNN><letter>.png (letter preserves intra-line order), and emit one
    mapping.json line: {order, text, duration_s, mode, frames:[{file, image,
    flip, blur_regions, bbox}]}.

    The pipeline manifest (consumed by s6 audio + s7 video) gets ONE beat per
    LINE so audio stays one-narration-per-line. The beat's visible image is the
    line's FIRST frame - enough for the current s7 to render a valid baseline
    video. To honour sequence/together, s7 reads mapping.json (see README:
    "What s7 must change").
    """
    data = request.get_json(silent=True) or {}
    chapter_id = _sanitize_chapter(data.get("chapter_id") or "")
    entries = data.get("entries") or []
    if not entries:
        return jsonify(error="No entries to export."), 400

    strip_path = _strip_path(chapter_id)
    if not strip_path.exists():
        return jsonify(error=f"No strip for {chapter_id}; load a PDF first."), 400
    strip = Image.open(strip_path).convert("RGB")

    frames_dir = chapter_work_dir(chapter_id) / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    mapping = []
    beats = []
    total_frames = 0
    for i, entry in enumerate(entries, start=1):
        frames = [f for f in (entry.get("frames") or []) if f.get("bbox")]
        if not frames:
            continue  # a line with no frames bound yet - skip it

        line_text = (entry.get("line_text") or "").strip()
        duration_s = float(entry.get("duration_s") or 4.0)
        mode = "together" if entry.get("mode") == "together" else "sequence"

        out_frames = []
        for j, fr in enumerate(frames):
            frame_name = f"frame_{i:03d}{_frame_suffix(j)}.png"
            frame_path = frames_dir / frame_name
            _crop_frame(strip, fr, frame_path)
            total_frames += 1
            out_frames.append({
                "file": frame_name,
                "image": str(frame_path),
                "flip": bool(fr.get("flip")),
                "blur_regions": [list(_region_box_style(r)) for r in (fr.get("blur") or [])],
                "bbox": [int(round(v)) for v in fr["bbox"]],
            })

        mapping.append({
            "order": i,
            "text": line_text,
            "duration_s": duration_s,
            "mode": mode,
            "frames": out_frames,
        })
        # One beat per LINE (one narration -> one audio). The visible image is the
        # first frame; has_face so downstream treats it as a real character frame.
        beats.append(Panel(
            id=f"L{i:03d}",
            bbox=[float(v) for v in frames[0]["bbox"]],
            order=i,
            crop=out_frames[0]["image"],
            script_line=line_text,
            duration_s=duration_s,
            selected=True,
            has_person=True,
            has_face=True,
            is_text_only=False,
        ))

    if not beats:
        return jsonify(error="No line had a frame bound. Draw a box first."), 400

    framer_dir = _framer_dir(chapter_id)
    mapping_path = framer_dir / "mapping.json"
    mapping_path.write_text(
        json.dumps({"chapter_id": chapter_id, "lines": mapping}, indent=2,
                   ensure_ascii=False),
        encoding="utf-8",
    )

    # A manifest the orchestrator can resume from. stage="clean" means the next
    # stages to run are audio (s6) then video (s7); everything before is skipped.
    m = Manifest(
        chapter_id=chapter_id,
        source_pdf=str(framer_dir / "_upload.pdf"),
        work_dir=str(chapter_work_dir(chapter_id)),
        doc_type="webtoon",
        stage="clean",
        pages=[Page(index=1, image=str(strip_path))],
        beats=beats,
    )
    m.save()

    slug, ch_no = _scope_params()
    if slug and ch_no:
        projects.set_status(slug, ch_no, "boxed")
        projects.write_media_snapshot(slug, ch_no)

    return jsonify(
        chapter_id=chapter_id,
        lines_written=len(beats),
        frames_written=total_frames,
        frames_dir=str(frames_dir),
        mapping_path=str(mapping_path),
        manifest_path=str(m.path),
        next_command=f"python orchestrator.py --chapter {chapter_id} --from audio",
    )


# --------------------------------------------------------------------------- #
# generate audio + video — run the pipeline's s6/s7 as a background subprocess
# and STREAM its stdout to the browser (Server-Sent Events) so the long TTS
# model-load + per-line synth shows live, never a frozen request.
# --------------------------------------------------------------------------- #
# One job per chapter (value is the live Popen, or None while it's being spawned).
_GEN_JOBS: dict[str, "subprocess.Popen | None"] = {}
_GEN_LOCK = threading.Lock()


def _sse(obj: dict) -> str:
    """Format one Server-Sent Events 'message' frame."""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _beat_count(chapter_id: str) -> int:
    """How many narration lines (beats) this chapter has, for the progress bar."""
    try:
        return len(Manifest.load(chapter_work_dir(chapter_id)).beats)
    except Exception:  # noqa: BLE001 - missing/old manifest -> unknown total
        return 0


@app.get("/generate_stream")
def generate_stream():
    """Run the pipeline's audio+video, video-only, or audio-only for a chapter
    and stream progress as SSE. EventSource (GET) only, params in the query
    string:
        ?chapter=<id>&what=both|voice|video&project=<slug>&ch_no=<n>
    `project`+`ch_no` scope the run to projects/<slug>/chapters/<n>/{work,output}
    (see config.chapter_scope) - required for a project chapter; the generator
    re-enters the scope explicitly (see note above _scope_params) because the
    request context is gone by the time this generator body actually runs.
    `what=both` runs s6 (TTS) then s7 (assemble) via `--from audio`; `what=voice`
    re-runs ONLY s6 via `--from audio --to audio` (e.g. after changing the
    project's voice sample or speed, without re-selecting frames) - it never
    touches recap.mp4 or the chapter's status, so a stale render stays valid
    until you also re-render video; `what=video` re-renders with s7 only via
    `--from video`. Emits {type:start|log|done|error} frames; `done` carries
    the /output/<ch>/recap.mp4 URL when a video stage actually ran and produced
    one."""
    chapter_id = _sanitize_chapter(request.args.get("chapter", ""))
    what = (request.args.get("what") or "both").strip().lower()
    if what == "video":
        from_stage, to_stage = "video", None
    elif what == "voice":
        from_stage, to_stage = "audio", "audio"
    else:
        what = "both"
        from_stage, to_stage = "audio", None
    slug, ch_no = _scope_params()
    job_key = f"{slug}/{ch_no}" if slug and ch_no else chapter_id
    video_url = (f"/output/{chapter_id}/recap.mp4?project={slug}&ch_no={ch_no}"
                 if slug and ch_no else f"/output/{chapter_id}/recap.mp4")

    work_dir = projects.chapter_work_dir(slug, ch_no) if slug and ch_no else chapter_work_dir(chapter_id)
    output_dir = projects.chapter_output_dir(slug, ch_no) if slug and ch_no else chapter_output_dir(chapter_id)
    manifest = work_dir / "manifest.json"

    def stream():
        with config.chapter_scope(work_dir, output_dir):
            if not manifest.exists():
                yield _sse({"type": "error",
                            "message": f"No manifest for {chapter_id}. Export from the "
                                       f"Framer first."})
                return

            # reserve the per-chapter slot WITHOUT yielding under the lock
            with _GEN_LOCK:
                busy = job_key in _GEN_JOBS
                if not busy:
                    _GEN_JOBS[job_key] = None
            if busy:
                yield _sse({"type": "error",
                            "message": f"A generate job is already running for "
                                       f"{chapter_id}."})
                return

            total = _beat_count(chapter_id)
            label = {"both": "audio + video", "voice": "audio (voice only)",
                     "video": "video"}[what]
            yield _sse({"type": "start", "what": what, "total_beats": total,
                        "message": f"Starting {label} for {chapter_id} "
                                   f"({total or '?'} line(s))…"})

            if slug and ch_no:
                # a settings change since the last generate must still land
                projects.write_media_snapshot(slug, ch_no)

            # -u + PYTHONUNBUFFERED so each print streams immediately; utf-8 so
            # the child never dies on a unicode arrow and we decode the pipe
            # cleanly. MANHWA_WORK_ROOT/OUTPUT_ROOT hand the same scope to the
            # orchestrator subprocess (see config.py's env-var fallback).
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            if slug and ch_no:
                env["MANHWA_WORK_ROOT"] = str(work_dir)
                env["MANHWA_OUTPUT_ROOT"] = str(output_dir)
            cmd = [sys.executable, "-u", str(ROOT / "orchestrator.py"),
                   "--chapter", chapter_id, "--from", from_stage]
            if to_stage:
                cmd += ["--to", to_stage]
            proc = None
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(ROOT), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, text=True, bufsize=1,
                    encoding="utf-8", errors="replace")
                with _GEN_LOCK:
                    _GEN_JOBS[job_key] = proc

                yield _sse({"type": "log", "line": "$ " + " ".join(cmd)})
                for line in proc.stdout:                       # streams as it arrives
                    yield _sse({"type": "log", "line": line.rstrip("\n")})
                proc.wait()

                if proc.returncode == 0:
                    if what == "voice":
                        # audio-only: no video ran, so recap.mp4/status are
                        # untouched - a stale render stays valid until
                        # "Re-render video" is run over the new audio.
                        yield _sse({"type": "done", "ok": True, "video": None,
                                    "message": "Audio regenerated. Re-render video "
                                               "to hear it in the recap."})
                    else:
                        mp4 = output_dir / "recap.mp4"
                        if mp4.exists() and slug and ch_no:
                            projects.set_status(slug, ch_no, "rendered")
                        yield _sse({"type": "done", "ok": True,
                                    "video": (video_url if mp4.exists() else None),
                                    "message": ("Done." if mp4.exists()
                                                else "Finished, but recap.mp4 was not found.")})
                else:
                    yield _sse({"type": "done", "ok": False,
                                "message": f"Pipeline exited with code {proc.returncode}."})
            except GeneratorExit:                              # client disconnected / Stop
                if proc and proc.poll() is None:
                    proc.terminate()
                raise
            except Exception as e:  # noqa: BLE001 - report, don't crash the stream
                yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:  # noqa: BLE001
                        pass
                with _GEN_LOCK:
                    _GEN_JOBS.pop(job_key, None)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",      # don't let proxies buffer
                             "Connection": "keep-alive"})


@app.get("/output/<chapter_id>/<path:name>")
def output_file(chapter_id: str, name: str):
    """Serve a finished artifact (e.g. recap.mp4) for in-page play/download. Flask's
    send_from_directory honours Range requests, so the <video> element scrubs."""
    out_dir = chapter_output_dir(_sanitize_chapter(chapter_id))
    if not (out_dir / name).exists():
        return jsonify(error=f"{name} not found for this chapter."), 404
    return send_from_directory(str(out_dir), name)


@app.post("/open_folder")
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
    if not any(_is_under(p, root) for root in allowed_roots):
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


# --------------------------------------------------------------------------- #
# editor state (per-chapter framer working state) + autosave recovery.
#
# NOTE: this is the FRAMER's own editing session (lines/frames/blur/etc.) - a
# different concept from the "project" system in projects.py (a whole manhwa
# series with a chapter list). Named /editor_state/* to avoid colliding with
# the new /api/projects/* endpoints.
# --------------------------------------------------------------------------- #
def _editor_state_path(chapter_id: str, recovery: bool) -> Path:
    return _framer_dir(chapter_id) / ("recovery.json" if recovery else "editor_state.json")


@app.post("/editor_state/save")
def save_editor_state():
    """Persist the FULL working state (lines, frames, modes, durations, strip ref).

    Body: JSON {chapter_id, recovery?:bool, state:{...}, project?, ch_no?}.
    `project`/`ch_no` are ONLY the chapter-scope params (see _scope_params) -
    the state blob itself is carried under `state`, deliberately NOT `project`,
    so it can never collide with the scope param of the same request. `recovery`
    writes the periodic autosave (recovery.json) so a closed tab never loses
    work; a normal save writes editor_state.json. The blob is stored verbatim
    plus a stamp.
    """
    data = request.get_json(silent=True) or {}
    chapter_id = _sanitize_chapter(data.get("chapter_id") or "")
    state = data.get("state")
    if not chapter_id or not isinstance(state, dict):
        return jsonify(error="chapter_id and state required"), 400
    recovery = bool(data.get("recovery"))
    state = dict(state)
    state["chapter_id"] = chapter_id
    state["saved_at"] = _now_iso()
    path = _editor_state_path(chapter_id, recovery)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify(ok=True, path=str(path), saved_at=state["saved_at"])


@app.get("/editor_state/load")
def load_editor_state():
    """Restore a saved session: returns {state, preview}.

    Query: chapter, recovery?(0/1). `preview` is the same payload /load_pdf returns
    (rebuilt from the persisted tiles, NO re-stitch) so the strip is shown again;
    it is null if the chapter has no stitched strip on disk.
    """
    chapter_id = _sanitize_chapter(request.args.get("chapter", ""))
    recovery = request.args.get("recovery") in ("1", "true", "yes")
    path = _editor_state_path(chapter_id, recovery)
    if not path.exists():
        return jsonify(error=f"No saved {'recovery' if recovery else 'editor'} state "
                             f"for {chapter_id}."), 404
    state = json.loads(path.read_text(encoding="utf-8"))
    return jsonify(state=state, preview=_preview_payload(chapter_id))


@app.get("/editor_state/status")
def editor_state_status():
    """Report whether a chapter has a saved state / newer recovery (for the
    'restore unsaved work?' prompt). Returns {project:{exists,mtime},
    recovery:{exists,mtime}}."""
    chapter_id = _sanitize_chapter(request.args.get("chapter", ""))
    out = {}
    for key, recovery in (("project", False), ("recovery", True)):
        p = _editor_state_path(chapter_id, recovery)
        out[key] = {"exists": p.exists(), "mtime": (p.stat().st_mtime if p.exists() else 0)}
    return jsonify(**out)


@app.get("/settings")
def get_settings():
    """Current session UI settings (theme + split position), held server-side."""
    return jsonify(**_SETTINGS)


@app.post("/settings")
def set_settings():
    """Update session UI settings. Body: JSON {theme?:'dark'|'light', split?:int}."""
    data = request.get_json(silent=True) or {}
    if data.get("theme") in ("dark", "light"):
        _SETTINGS["theme"] = data["theme"]
    if "split" in data:
        try:
            # Sanity bounds only - the real clamp to the viewport happens
            # client-side in applySplit()/splitMax(), which is allowed to go
            # much wider than this on a large monitor.
            _SETTINGS["split"] = max(240, min(6000, int(data["split"])))
        except (TypeError, ValueError):
            pass
    return jsonify(**_SETTINGS)


# --------------------------------------------------------------------------- #
# in-app update - "Check for updates" pulls the app's own CODE from git.
# Deliberately just `git pull --ff-only` in ROOT: nothing here touches .venv,
# installed packages, or the Hugging Face model cache - those all live
# outside what git tracks (see .gitignore), so a pull can never reach them.
# GIT_TERMINAL_PROMPT=0 makes a missing/expired credential fail fast with a
# clear error instead of hanging the request on an invisible auth prompt.
# --------------------------------------------------------------------------- #
@app.post("/api/update")
def api_update():
    if not (ROOT / ".git").exists():
        return jsonify(ok=False, not_git=True,
                        message="This install wasn't set up with git, so in-app "
                                "updates aren't available here. Reinstall with "
                                "the latest installer to get this feature.")

    def run_git(*args):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        return subprocess.run(["git", *args], cwd=str(ROOT), env=env,
                               capture_output=True, text=True, timeout=120)

    try:
        old = run_git("rev-parse", "HEAD")
        if old.returncode != 0 and "dubious ownership" in (old.stderr or "").lower():
            # Windows: git refuses to operate on a repo whose folder ownership
            # looks unexpected (seen on some drives/installs, e.g. an install
            # under G:\...) - self-heal by trusting this exact folder for this
            # user, then retry once. Reactive (not run on every request) so it
            # never bloats the user's global .gitconfig on the happy path.
            subprocess.run(["git", "config", "--global", "--add", "safe.directory",
                             str(ROOT).replace("\\", "/")],
                            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
                            capture_output=True, text=True, timeout=30)
            old = run_git("rev-parse", "HEAD")
        if old.returncode != 0:
            return jsonify(ok=False, message="Could not read the current version: "
                                              + (old.stderr or old.stdout).strip())
        old_hash = old.stdout.strip()

        pull = run_git("pull", "--ff-only")
        if pull.returncode != 0:
            return jsonify(ok=False, message="Update failed: "
                                              + (pull.stderr or pull.stdout).strip())

        new = run_git("rev-parse", "HEAD")
        new_hash = new.stdout.strip() if new.returncode == 0 else old_hash

        if new_hash == old_hash:
            return jsonify(ok=True, already_up_to_date=True, files_changed=0,
                            requirements_changed=False, message="Already up to date.")

        diff = run_git("diff", "--name-only", old_hash, new_hash)
        changed = [ln for ln in (diff.stdout or "").splitlines() if ln.strip()]
        requirements_changed = "requirements.txt" in changed

        if requirements_changed:
            message = (f"Updated {len(changed)} file(s). Dependencies changed - "
                       f"please re-run setup.bat, then restart the app.")
        else:
            message = f"Updated {len(changed)} file(s). Restart the app to apply."

        return jsonify(ok=True, already_up_to_date=False, files_changed=len(changed),
                        requirements_changed=requirements_changed, message=message)
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, message="Update timed out - check your internet "
                                          "connection and try again.")
    except FileNotFoundError:
        return jsonify(ok=False, message="git is not installed or not on PATH.")
    except Exception as e:  # noqa: BLE001 - report, don't crash the app
        return jsonify(ok=False, message=f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# project system - list/create/open projects, chapter status, media settings +
# live previews. See tools/framer/projects.py (storage) and webtoon_meta.py
# (the best-effort series scraper). Chapter-scoped routes above are entered via
# the before_request hook near the top of this file whenever a request carries
# `project` + `ch_no`.
# --------------------------------------------------------------------------- #
@app.get("/api/projects")
def api_list_projects():
    return jsonify(projects=projects.list_projects())


@app.post("/api/projects")
def api_create_project():
    """Body: JSON {name, url}. Creates the project, then tries to auto-fetch
    series details from webtoons.com (title, chapter count, chapter list).
    NEVER blocks on a failed fetch - `fetch.ok` tells the UI whether to fall
    back to the manual "enter chapter range 1..N" input."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    url = (data.get("url") or "").strip()
    if not name:
        return jsonify(error="Project name required."), 400
    project = projects.create(name, url)

    fetched = webtoon_meta.fetch_series(url) if url else None
    if fetched:
        project["series_title"] = fetched["title"]
        project["source"] = "scraped"
        projects.fill_chapters(project, fetched["chapters"])
        projects.save(project)
        fetch_result = {"ok": True, "title": fetched["title"], "total": fetched["total"]}
    else:
        fetch_result = {"ok": False, "reason": "Couldn't read the series page - enter "
                                                "the chapter range manually."}

    return jsonify(slug=project["slug"], project=project, fetch=fetch_result)


@app.get("/api/projects/<slug>")
def api_get_project(slug):
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    project = projects.load(slug)
    for ch in project["chapters"]:
        try:
            projects.derive_status(slug, ch["n"])
        except Exception:  # noqa: BLE001 - a derive glitch must not break the page
            pass
    return jsonify(project=projects.load(slug))


@app.delete("/api/projects/<slug>")
def api_delete_project(slug):
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    projects.delete(slug)
    return jsonify(ok=True)


@app.post("/api/projects/<slug>/chapters")
def api_set_chapters(slug):
    """Body: JSON {from, to} for the manual fallback range, or {refetch:true} to
    retry the webtoons.com scrape."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    data = request.get_json(silent=True) or {}
    project = projects.load(slug)

    if data.get("refetch"):
        fetched = webtoon_meta.fetch_series(project["url"])
        if not fetched:
            return jsonify(error="Couldn't read the series page.", ok=False), 200
        project["series_title"] = fetched["title"]
        project["source"] = "scraped"
        projects.fill_chapters(project, fetched["chapters"])
        projects.save(project)
        return jsonify(ok=True, project=project)

    try:
        start, end = int(data.get("from")), int(data.get("to"))
    except (TypeError, ValueError):
        return jsonify(error="from and to must be chapter numbers."), 400
    if start < 1 or end < start:
        return jsonify(error="Invalid chapter range."), 400
    project["source"] = "manual"
    projects.manual_range(project, start, end)
    projects.save(project)
    return jsonify(ok=True, project=project)


@app.post("/api/projects/<slug>/chapters/<n>/status")
def api_set_chapter_status(slug, n):
    """Body: JSON {status, force?:bool}. `force` is used by "Mark complete" and
    its reset action, which are manual overrides outside the normal ranking."""
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    try:
        ch = projects.set_status(slug, n, status, force=bool(data.get("force")))
    except (ValueError, KeyError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, chapter=ch)


@app.get("/api/projects/<slug>/settings")
def api_get_settings(slug):
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    return jsonify(settings=projects.load(slug)["settings"])


@app.post("/api/projects/<slug>/settings")
def api_set_settings(slug):
    """Body: JSON settings block (partial - deep-merged). Clamps sliders and
    rewrites media_settings.json for every chapter that already has a work
    dir, so a saved change reaches renders immediately."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    data = request.get_json(silent=True) or {}
    project = projects.update_settings(slug, data)
    return jsonify(ok=True, settings=project["settings"])


@app.post("/api/projects/<slug>/asset")
def api_upload_asset(slug):
    """Multipart: {kind: music|voice|watermark|background, file}. Saves the
    file under projects/<slug>/assets/ and points the matching settings field
    at it; does NOT switch mode/enabled by itself (stays whatever the
    settings panel already has - selecting "custom" is a separate action)."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    kind = (request.form.get("kind") or "").strip()
    file_storage = request.files.get("file")
    if not file_storage or not file_storage.filename:
        return jsonify(error="file required"), 400
    try:
        rel_path = projects.save_asset(slug, kind, file_storage.filename, file_storage)
    except ValueError as e:
        return jsonify(error=str(e)), 400

    project = projects.load(slug)
    if kind == "music":
        project["settings"]["music"]["file"] = rel_path
    elif kind == "voice":
        project["settings"]["voice"]["file"] = rel_path
    elif kind == "watermark":
        project["settings"]["watermark"]["file"] = rel_path
    elif kind == "background":
        project["settings"]["background"]["file"] = rel_path
    projects.save(project)
    projects.write_all_media_snapshots(slug)
    return jsonify(ok=True, path=rel_path, name=Path(rel_path).name)


@app.delete("/api/projects/<slug>/asset/<kind>")
def api_delete_asset(slug, kind):
    """Revert `kind` to its default (no file)."""
    if kind not in projects.VALID_ASSET_KINDS:
        return jsonify(error=f"unknown asset kind: {kind}"), 400
    asset = projects.find_asset(slug, kind)
    if asset:
        asset.unlink(missing_ok=True)
    project = projects.load(slug)
    if kind == "music":
        project["settings"]["music"] = {"mode": "default", "file": None}
    elif kind == "voice":
        project["settings"]["voice"] = {"mode": "default", "file": None}
    elif kind == "watermark":
        project["settings"]["watermark"]["file"] = None
    elif kind == "background":
        project["settings"]["background"] = {"mode": "blur", "file": None, "dim": 0.85}
    projects.save(project)
    projects.write_all_media_snapshots(slug)
    return jsonify(ok=True)


@app.get("/api/projects/<slug>/asset/<kind>")
def api_get_asset(slug, kind):
    asset = projects.find_asset(slug, kind)
    if not asset:
        return jsonify(error=f"No {kind} asset saved for this project."), 404
    return send_from_directory(str(asset.parent), asset.name)


# --------------------------------------------------------------------------- #
# settings live previews - built with the SAME s7 compose/watermark helpers
# used by the real render, so the preview is WYSIWYG by construction. Settings
# are read from QUERY PARAMS (the in-progress form state), except an
# image file, which must already be uploaded (see /asset above) since a
# multi-megabyte file isn't practical to pass on every debounced preview tick.
# --------------------------------------------------------------------------- #
def _placeholder_frame(slug: str) -> Path:
    """Any already-exported frame for this project to preview settings over,
    else a simple generated character silhouette, cached on first use."""
    chapters_dir = projects.project_dir(slug) / "chapters"
    if chapters_dir.is_dir():
        for frame in sorted(chapters_dir.glob("*/work/frames/frame_*.png")):
            return frame
    placeholder = projects.assets_dir(slug) / "_sample_placeholder.png"
    if not placeholder.exists():
        from PIL import ImageDraw
        img = Image.new("RGB", (700, 1000), (58, 66, 86))
        d = ImageDraw.Draw(img)
        d.ellipse((210, 110, 490, 410), fill=(214, 196, 180))
        d.rounded_rectangle((150, 420, 550, 900), radius=48, fill=(96, 108, 140))
        img.save(placeholder)
    return placeholder


def _png_response(img) -> Response:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/projects/<slug>/preview/watermark.png")
def api_preview_watermark(slug):
    """Query: enabled, type, text, opacity, size, spacing, angle. An image-type
    watermark uses the already-saved asset (see /asset).

    BUG FIX: this used to build a non-empty `cfg` dict regardless of the
    "Enabled" checkbox and never looked at it, so the preview always showed
    the watermark tiled even when it was OFF - the actual render (via
    media_settings_snapshot(), which correctly nulls the whole watermark
    block when disabled) would then have no watermark at all, exactly
    matching the "preview shows it, video doesn't" symptom. The preview now
    mirrors the real gate: no watermark in the preview when disabled."""
    enabled = (request.args.get("enabled", "1")).strip().lower() not in ("0", "false", "")
    sample = _placeholder_frame(slug)

    wm = None
    if enabled:
        cfg = {
            "type": request.args.get("type", "text"),
            "text": request.args.get("text", ""),
            "opacity": request.args.get("opacity", 0.12),
            "size": request.args.get("size", 0.12),
            "spacing": request.args.get("spacing", 1.0),
            "angle": request.args.get("angle", -30),
        }
        watermark_asset = projects.find_asset(slug, "watermark")
        cfg["file"] = str(watermark_asset) if watermark_asset else None
        wm = s7._build_watermark_layer({"watermark": cfg})

    # wm goes to the BACKGROUND layer only (see _compose_still), matching the
    # real render - so this preview accurately shows the watermark hidden
    # under the sample frame and visible only around/behind it.
    still = s7._compose_still(str(sample), wm=wm)
    return _png_response(still.resize((960, 540), Image.LANCZOS))


@app.get("/api/projects/<slug>/preview/background.png")
def api_preview_background(slug):
    """Query: dim. Uses the already-saved background asset (see /asset)."""
    bg_asset = projects.find_asset(slug, "background")
    dim = request.args.get("dim", 0.85)
    bg = s7._load_background({"background": {"file": str(bg_asset) if bg_asset else None,
                                              "dim": dim}})
    sample = _placeholder_frame(slug)
    still = s7._compose_still(str(sample), bg)
    return _png_response(still.resize((960, 540), Image.LANCZOS))


# --------------------------------------------------------------------------- #
# merge multiple chapters' rendered videos into one, in chapter order. Purely
# a framer-side feature over already-rendered recap.mp4 files - it reads
# finished output, nothing about the manifest/orchestrator/pipeline stages
# needs to change for this.
# --------------------------------------------------------------------------- #
_MERGE_JOBS: dict[str, "subprocess.Popen | None"] = {}
_MERGE_LOCK = threading.Lock()


def _ffprobe_info(path: Path) -> dict | None:
    """{"video": {width,height,codec_name}, "audio": {codec_name,sample_rate}}
    for `path`, or None on any failure (missing ffprobe, unreadable file).
    Used to decide whether a fast stream-copy merge is safe, or whether every
    input needs normalizing first (see merge_stream)."""
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error",
             "-show_entries", "stream=index,codec_type,width,height,codec_name,sample_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30)
        streams = json.loads(out.stdout or "{}").get("streams") or []
    except Exception:  # noqa: BLE001 - a bad probe must not crash the merge
        return None
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        return None
    return {"video": video, "audio": audio or {}}


def _write_concat_list(paths: list[Path], dest: Path) -> None:
    """ffmpeg concat-demuxer list file. Forward slashes side-step that format's
    own backslash-escaping rules, which Windows paths would otherwise trip."""
    lines = [f"file '{str(p).replace(chr(92), '/')}'" for p in paths]
    dest.write_text("\n".join(lines), encoding="utf-8")


def _build_merge_cmd_copy(ffmpeg: str, list_file: Path, out: Path) -> list[str]:
    """Fast path: every input already shares video+audio codec and resolution,
    so just concatenate the encoded bitstream - no re-encode, near-instant."""
    return [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(out)]


def _build_merge_cmd_reencode(ffmpeg: str, codec: str, paths: list[Path], out: Path) -> list[str]:
    """Safe path for mismatched inputs: scale/letterbox every video to
    1920x1080@24fps (never cropped or stretched) and resample audio to a
    common format before concatenating, so a stray non-standard render can't
    corrupt or desync the merge."""
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats"]
    for p in paths:
        cmd += ["-i", str(p)]
    parts, vlabels, alabels = [], [], []
    for i in range(len(paths)):
        parts.append(f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
                     f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p[v{i}]")
        parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        vlabels.append(f"[v{i}]")
        alabels.append(f"[a{i}]")
    concat_inputs = "".join(v + a for v, a in zip(vlabels, alabels))
    parts.append(f"{concat_inputs}concat=n={len(paths)}:v=1:a=1[vout][aout]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]"]
    if codec == "h264_nvenc":
        cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-b:v", "8M"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", str(out)]
    return cmd


@app.get("/api/projects/<slug>/merges")
def api_list_merges(slug):
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    return jsonify(merges=projects.load(slug).get("merges", []))


@app.get("/api/projects/<slug>/merge_stream")
def merge_stream(slug):
    """Concatenate several chapters' recap.mp4, in chapter order, into one
    video under projects/<slug>/merged/. EventSource (GET) only:
        ?chapters=1,3,5
    Only chapters currently marked "complete" (re-derived fresh from disk, not
    just the stored status) are accepted. When every input shares 1920x1080 +
    a matching video AND audio codec, the merge is a fast stream copy;
    otherwise every input is scaled/padded to 1920x1080@24fps first (see
    _build_merge_cmd_reencode) so a mismatched render can't corrupt the merge.
    Emits {type:start|log|done|error}; `done` carries the merged file's URL."""
    raw = (request.args.get("chapters") or "").strip()
    try:
        chapters = sorted({int(x) for x in raw.split(",") if x.strip()})
    except ValueError:
        chapters = []

    def stream():
        if not projects.exists(slug):
            yield _sse({"type": "error", "message": f"No such project: {slug}"})
            return
        if len(chapters) < 2:
            yield _sse({"type": "error", "message": "Select at least 2 complete chapters to merge."})
            return

        with _MERGE_LOCK:
            busy = slug in _MERGE_JOBS
            if not busy:
                _MERGE_JOBS[slug] = None
        if busy:
            yield _sse({"type": "error", "message": "A merge is already running for this project."})
            return

        proc = None
        try:
            yield _sse({"type": "start", "message": f"Checking {len(chapters)} chapter(s)…"})
            paths: list[Path] = []
            for n in chapters:
                ch = projects.get_chapter(projects.load(slug), n)
                if ch is None:
                    yield _sse({"type": "error", "message": f"No chapter {n} in this project."})
                    return
                try:
                    ch = projects.derive_status(slug, n)  # refuse a stale "complete" the disk disagrees with
                except KeyError:
                    yield _sse({"type": "error", "message": f"No chapter {n} in this project."})
                    return
                if ch["status"] != "complete":
                    yield _sse({"type": "error",
                                "message": f"Chapter {n} is not marked complete (status: {ch['status']})."})
                    return
                video = projects.chapter_video_path(slug, n)
                if not video.is_file():
                    yield _sse({"type": "error", "message": f"Chapter {n} has no rendered recap.mp4."})
                    return
                paths.append(video)

            yield _sse({"type": "log", "line": f"Probing {len(paths)} video(s)…"})
            infos = []
            for n, p in zip(chapters, paths):
                info = _ffprobe_info(p)
                if info is None:
                    yield _sse({"type": "error", "message": f"Could not read video info for chapter {n}."})
                    return
                infos.append(info)
                v, a = info["video"], info.get("audio") or {}
                yield _sse({"type": "log",
                            "line": f"  ch {n}: {v.get('width')}x{v.get('height')} {v.get('codec_name')} "
                                    f"/ audio {a.get('codec_name', '?')}@{a.get('sample_rate', '?')}"})

            uniform = (
                len({(i["video"].get("width"), i["video"].get("height"), i["video"].get("codec_name"))
                     for i in infos}) == 1
                and infos[0]["video"].get("width") == 1920 and infos[0]["video"].get("height") == 1080
                and len({(i.get("audio") or {}).get("codec_name") for i in infos}) == 1
                and len({(i.get("audio") or {}).get("sample_rate") for i in infos}) == 1
            )
            out_dir = projects.merged_dir(slug)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            name = f"merged_{stamp}_ch" + "-".join(str(n) for n in chapters) + ".mp4"
            out = out_dir / name
            ffmpeg = s7._ffmpeg()

            if uniform:
                yield _sse({"type": "log", "line": "All inputs are 1920x1080 with matching codecs - "
                                                    "fast stream-copy merge (no re-encode)."})
                list_file = out_dir / "_concat_list.txt"
                _write_concat_list(paths, list_file)
                cmd = _build_merge_cmd_copy(ffmpeg, list_file, out)
                mode = "copy"
            else:
                codec = "h264_nvenc" if s7._has_nvenc(ffmpeg) else "libx264"
                yield _sse({"type": "log",
                            "line": f"Inputs differ in resolution/codec - normalizing every clip to "
                                    f"1920x1080@24fps before merging ({codec}, slower)."})
                cmd = _build_merge_cmd_reencode(ffmpeg, codec, paths, out)
                mode = "reencode"

            yield _sse({"type": "log", "line": "$ " + " ".join(cmd)})
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    text=True, bufsize=1, encoding="utf-8", errors="replace")
            with _MERGE_LOCK:
                _MERGE_JOBS[slug] = proc
            for line in proc.stdout:
                yield _sse({"type": "log", "line": line.rstrip("\n")})
            proc.wait()

            if proc.returncode == 0 and out.is_file():
                projects.record_merge(slug, {"file": f"merged/{name}", "chapters": chapters,
                                             "mode": mode, "created_at": _now_iso()})
                url = f"/projects/{slug}/merged/{name}"
                yield _sse({"type": "done", "ok": True, "video": url,
                            "message": f"Merged {len(chapters)} chapter(s) -> {name}"})
            else:
                yield _sse({"type": "done", "ok": False,
                            "message": f"ffmpeg exited {proc.returncode}."})
        except GeneratorExit:                              # client disconnected / Stop
            if proc and proc.poll() is None:
                proc.terminate()
            raise
        except Exception as e:  # noqa: BLE001 - report, don't crash the stream
            yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
            with _MERGE_LOCK:
                _MERGE_JOBS.pop(slug, None)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


@app.get("/projects/<slug>/merged/<path:name>")
def merged_file(slug, name):
    """Serve a merged video for in-page play/download (Range-friendly, same
    as /output/<chapter_id>/<name>)."""
    d = projects.merged_dir(slug)
    if not (d / name).exists():
        return jsonify(error=f"{name} not found for this project."), 404
    return send_from_directory(str(d), name)


def main() -> None:
    host, port = "127.0.0.1", 5005
    url = f"http://{host}:{port}/"
    print("=" * 60)
    print("  Framer - manual frame selection")
    print(f"  Open: {url}")
    print("=" * 60)
    # threaded: a generate job holds an SSE connection open for minutes; other
    # requests (thumbnails, the result video, a second tab) must still be served.
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

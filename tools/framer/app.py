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

import contextlib
import difflib
import io
import json
import math
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# --- make the project root importable so we can REUSE stage 1 + the manifest --
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, Response, jsonify, request, send_file, send_from_directory  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

# Stage 1: importing it lifts Pillow's MAX_IMAGE_PIXELS guard (module-level) and
# gives us the exact stitch used by the real pipeline.
from stages import s1_pdf_to_pages as s1  # noqa: E402
import config  # noqa: E402 - module import so chapter_scope()/set_/clear_ are reachable
from config import chapter_work_dir, chapter_output_dir, WORK_DIR, OUTPUT_DIR, PROJECTS_DIR, NO_CONSOLE  # noqa: E402
from manifest import Manifest, Page, Panel  # noqa: E402
from atomic_io import atomic_write_json, clear_directory, read_json_with_backup_fallback  # noqa: E402
import gpu  # noqa: E402

import projects  # noqa: E402 - the project system (this package: tools/framer/projects.py)
import branding  # noqa: E402 - intro/outro for merged videos
import batch  # noqa: E402 - pipelined multi-chapter automation
import notify  # noqa: E402 - desktop notifications
import preflight  # noqa: E402 - checks before a batch starts
import claude_cli  # noqa: E402 - the local Claude Code CLI
import claude_usage  # noqa: E402 - subscription usage read from the CLI's rate-limit events
import claude_engine  # noqa: E402 - Claude script engine: panels -> narration blocks
import panel_split  # noqa: E402 - strip -> reading-order panels
import library  # noqa: E402 - per-project media library (sizes, deletion)
import project_defaults  # noqa: E402 - the settings new projects start with
import app_settings  # noqa: E402 - app-wide settings (the shared projects folder)
import bulk_download  # noqa: E402 - bulk chapter download for browser-only sources
import pacing  # noqa: E402 - per-line mood: where the narration pauses and how fast it runs
import tones  # noqa: E402 - chapter tones: script voice and speech delivery
import totals  # noqa: E402 - cached project totals: speaking pace, Claude tokens
import sites  # noqa: E402 - per-site adapters (tools/framer/sites/): metadata + chapter download
from stages import s7_assemble as s7  # noqa: E402 - reused by the settings live-preview routes
from stages import tts_kokoro  # noqa: E402 - the Kokoro voices and their preview

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

HERE = Path(__file__).resolve().parent
app = Flask(__name__)

# Session-wide UI settings, kept SERVER-SIDE in memory (NOT browser localStorage)
# so a theme / layout choice survives page reloads while the app is running.
_SETTINGS = {"theme": "dark", "split": None}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sse(obj: dict) -> str:
    """Format one Server-Sent Events 'message' frame. Shared by every SSE route
    (/gemini_parts_stream, /api/projects/<slug>/merge_stream) - the old
    /generate_stream route also used to use this before it moved to a polled
    job queue (see the _JOBS note below), but the other two routes never
    stopped needing it."""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


# --------------------------------------------------------------------------- #
# project scoping - every route that touches chapter files redirects
# chapter_work_dir()/chapter_output_dir() (see config.py) to
# projects/<slug>/chapters/<n>/{work,output} for the duration of the request,
# by reading `project` + `ch_no` (query args, JSON body, or form field - never
# collides with the pre-existing `chapter`/`chapter_id` label param).
#
# NOTE on /gemini_parts_stream (SSE) and the render job worker thread
# (_run_job, near /api/generate): Flask tears the request context down
# before a streamed generator's body actually runs, and the job worker isn't
# in a request context AT ALL (it's a persistent background thread, so a job
# outlives the request that enqueued it - see the note above _JOBS). Neither
# can rely on before_request's scope, so both compute their own
# (work_dir, output_dir) up front and wrap their body in an explicit
# `with config.chapter_scope(...)`.
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
    """The framer's folder for the chapter. Not created here: writers create it (see _ensure_dir)."""
    return chapter_work_dir(chapter_id) / "framer"


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


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
    return Image.open(_strip_path(chapter_id))


# --------------------------------------------------------------------------- #
# download a chapter (site adapter, see tools/framer/sites/) + split for Gemini
# --------------------------------------------------------------------------- #
def _download_dir(chapter_id: str) -> Path:
    d = chapter_work_dir(chapter_id) / "download"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _downloaded_pdf(chapter_id: str) -> Path | None:
    """The chapter's already-downloaded PDF (see /download), if any - most
    recently written, since an adapter may nest it under a subfolder."""
    pdfs = sorted(_download_dir(chapter_id).rglob("*.pdf"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
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


def _existing_gemini_parts(chapter_id: str) -> list[dict]:
    """Parts already on disk for `chapter_id`, in the same shape
    /gemini_parts_stream's `done` event uses (see _build_gemini_parts_stream)."""
    parts = []
    for p in sorted(_gemini_parts_dir(chapter_id).glob("*.pdf")):
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
        return _make_preview(Image.open(strip_path).convert("RGB"), chapter_id)

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


@app.get("/__app_ping__")
def app_ping():
    """Lightweight identity marker - NOT app data. Used to tell "a previous
    ReCapper instance is already on this port" apart from "some unrelated
    program happens to be using it" (see _resolve_port at startup), and to
    poll for the server coming back up after /api/restart."""
    return jsonify(app="recapper", pid=os.getpid())


@app.get("/assets/<path:name>")
def app_asset(name):
    """Serves the app's own branding assets (logo/icon) from ROOT/assets -
    used by index.html for the favicon and header logo."""
    return send_from_directory(str(ROOT / "assets"), name)


class Refused(Exception):
    """A request the app will not run; `status` is the HTTP status a route reports for it."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _download_chapter(url: str, chapter_no: str, raw_id: str, slug: str | None, ch_no: str | None,
                      log: Callable[[str], None] | None = None) -> dict:
    """Download ONE chapter as a PDF from whichever site adapter (see tools/framer/sites/)
    matches `url`; `log` also receives each step as it happens. Returns {chapter_id, pdf,
    page_count, steps}."""
    if not url:
        raise Refused("Paste a webtoons.com, kingofshojo.com or mgeko.cc chapter URL.")
    adapter = sites.detect(url)
    if adapter is None:
        raise Refused("That doesn't look like a webtoons.com, kingofshojo.com or mgeko.cc URL.")
    if not raw_id and not chapter_no:
        raise Refused("Enter the chapter number (or a chapter id).")
    chapter_id = _sanitize_chapter(raw_id or chapter_no)

    dest = _download_dir(chapter_id)
    steps = [f"Downloading {adapter.SITE_NAME} chapter {chapter_no or '(from URL)'} "
             f"into {dest} …"]
    def note(line: str) -> None:
        steps.append(line)
        if log:
            log(line)

    try:
        pdf = _fetch_chapter_pdf(adapter, url, chapter_no, dest, slug, ch_no, note)
    except sites.DownloadTimeout as e:
        raise Refused(str(e), 504) from e
    except Exception as e:  # noqa: BLE001 - surface the adapter's own message to the UI
        raise Refused(str(e), 500) from e

    for old in dest.rglob("*"):
        if old.is_file() and old != pdf:
            old.unlink()
    pages = _pdf_page_count(pdf)
    steps.append(f"Downloaded {pdf.name} — {pages} page(s):\n    {pdf}")
    if slug and ch_no:
        projects.set_status(slug, ch_no, "downloaded")
    return {"chapter_id": chapter_id, "pdf": str(pdf), "page_count": pages, "steps": steps}


def _fetch_chapter_pdf(adapter, url: str, chapter_no: str, dest: Path, slug: str | None, ch_no: str | None,
                       log: Callable[[str], None]) -> Path:
    """The chapter's PDF in `dest`. A project that remembers its source keeps its downloads in the
    project folder: a chapter stored there is copied from it instead of being downloaded again, and a
    fresh download is stored there once complete."""
    keeps_copy = bool(slug and ch_no and projects.load(slug)["site"])
    stored = projects.stored_pdf(slug, ch_no) if keeps_copy else None
    if stored:
        log(f"Using the copy in the project folder ({stored.name})")
        return Path(shutil.copy2(stored, dest / stored.name))
    pdf = adapter.download_chapter(url, chapter_no, dest, log=log)
    if keeps_copy:
        projects.store_pdf(slug, ch_no, pdf)
    return pdf


@app.post("/download")
def download():
    """Download ONE chapter as a PDF. Gemini-part splitting/compression is a SEPARATE step
    (see /gemini_parts_stream) so its progress can stream.

    Body: JSON {url, chapter_no?, chapter_id?}. Returns {chapter_id, pdf, page_count,
    steps:[...]} - `pdf` is the full chapter PDF that feeds BOTH Stitch PDF (unchanged)
    and Gemini-part prep."""
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(_download_chapter((data.get("url") or "").strip().strip('"'),
                                         str(data.get("chapter_no") or "").strip(),
                                         (data.get("chapter_id") or "").strip(), *_scope_params()))
    except Refused as e:
        return jsonify(error=str(e)), e.status


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
        # Outer catch-all: ANY exception escaping here (even one from
        # entering config.chapter_scope, or a bug in code below that isn't
        # one of the two anticipated failure points) would otherwise abort
        # the HTTP response with no SSE payload at all - the browser's
        # EventSource can't read a body off a dropped/never-started
        # connection, so it just reports a generic "disconnected" and the
        # REAL error (visible only in the server's own console/log) never
        # reaches the user. Converting every failure into a proper
        # {type:"error"} frame is what actually lets the UI show it.
        try:
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
                for item in _build_gemini_parts_stream(pdf_path, chapter_id):
                    if item["type"] == "result":
                        yield _sse({"type": "done", "ok": True,
                                    "parts": item["parts"], "dir": item["dir"]})
                    else:
                        yield _sse(item)
        except GeneratorExit:
            raise
        except Exception as e:  # noqa: BLE001 - last-resort: still report, don't crash the stream
            yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


# --------------------------------------------------------------------------- #
# Gemini script generation - reads the SAME gemini_parts/*.pdf files the panel
# above produces (never re-splits anything), sends each part to the Gemini
# API in order, and stitches the results into one script - replacing the old
# manual "upload each part to Gemini yourself, paste the answer back" step.
# --------------------------------------------------------------------------- #
GEMINI_NARRATION_TYPES = {
    "first_person": 'first-person, as the hero living through these events ("I", "me")',
    "third_person": 'third-person recap narration ("he", "she", "they")',
}
# Pro is the default: this app cares more about faithful dialogue extraction
# and prose quality than speed. Flash is offered as a same-quality-tier
# fallback for when Pro's (lower) free-tier rate limit is the bottleneck -
# see GEMINI_MIN_INTERVAL_S below, which paces requests generously for
# either, and _gemini_generate_part's own 429 retry/backoff on top of that.
GEMINI_MODELS = {
    "gemini-3.1-pro-preview": "Gemini 3.1 Pro - highest quality (slower, lower rate limit)",
    "gemini-3.8-flash": "Gemini 3.8 Flash - faster (higher rate limit)",
}
GEMINI_DEFAULT_MODEL = "gemini-3.1-pro-preview"


def _gemini_min_interval(model: str) -> float:
    return 8.0 if "pro" in model else 3.0


def _gemini_client():
    """Never raises anything but a plain RuntimeError with a fix-it message -
    this reaches the SSE stream as a single clear {type:"error"} frame."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:  # noqa: BLE001
        pass
    key, _source = _get_gemini_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set. Add it in Settings (Script "
                            "generation) or to a .env file in the project root.")
    from google import genai
    return genai.Client(api_key=key)


def _narration_voice(tone: str, narration_type: str) -> str:
    narr_desc = GEMINI_NARRATION_TYPES.get(narration_type, GEMINI_NARRATION_TYPES["first_person"])
    return f"Narrate {narr_desc}.\n\n{tones.style_instruction(tone)}"


def _style_reference_directive(style_reference: str) -> str:
    return ("Match the LANGUAGE, TONE, and HUMOR of this example script as closely "
            "as you can - imitate its voice and style, not its plot or content:\n\n"
            + style_reference.strip())


def _gemini_system_prompt(tone: str, narration_type: str, style_reference: str,
                           is_first_part: bool, is_last_part: bool) -> str:
    parts = [
        "You are writing a recap narration script for one part of a manhwa/webtoon "
        "chapter, from the attached PDF (its pages are the comic's panels, in "
        "reading order, including all dialogue and sound effects as they appear).",
        _narration_voice(tone, narration_type),
        "Extract EVERY piece of dialogue and every story beat faithfully - miss "
        "nothing important - but tell it as flowing narration prose, not a "
        "transcript: weave dialogue and description together naturally, the way "
        "a narrator retelling the chapter out loud would.",
        "Narrate like a skilled recap-channel storyteller, adding understanding and energy "
        "rather than reading panels out. Give every meaningful panel its due: a fight may be "
        "compressed into flowing narration, but each hit, turn, reveal and reaction that "
        "matters is still told. Besides what happens, say why it matters and what is at stake, "
        "what a character wants, feels or is thinking when the pages show it, how a rule or "
        "power of this world works when a scene shows one, and a brief reminder of an earlier "
        "event when it bears on the moment; build tension toward a turn and react when a "
        "reveal lands. There is no target length; the story sets it. The only filler to leave "
        "out is repeating an event in other words, inventing events or inner thoughts the "
        "pages do not show, generic hype (\"you won't believe what happens next\"), and "
        "stretching one panel over sentences that add nothing.",
        "Write for the ear: end each thought with a full stop and use a comma only where a "
        "speaker would pause; no dashes, ellipses, colons, semicolons or parentheses, and no "
        "quotation marks in the middle of a sentence.",
        "Begin every paragraph with a delivery tag in square brackets, one of "
        + ", ".join(f"[{name}]" for name in pacing.PACES) + ": calm for ordinary narration "
        "(most paragraphs), tense for suspense and confrontation, action for fights and fast "
        "events, reveal for a surprise or discovery (put the revelation in the paragraph's "
        "last sentence), sad for grief and quiet emotion, comic for a joke (put the punchline "
        "last). Start a new paragraph whenever the delivery changes.",
        "Output ONLY the narration prose itself, in reading order, each paragraph starting with "
        "its tag - no scene/part headings, no markdown, no bullet points, no meta-commentary "
        "about the images or the task.",
    ]
    if style_reference.strip():
        parts.append(_style_reference_directive(style_reference))
    if not is_first_part:
        parts.append(
            "This PDF starts by repeating the last few pages of the previous part "
            "(included only so you have context for continuity). Do NOT re-narrate "
            "that repeated part - continue the story smoothly from where the new "
            "content begins, as if this were one uninterrupted chapter."
        )
    if not is_last_part:
        parts.append(
            "This is not the final part of the chapter - do not write an ending, "
            "conclusion, or wrap-up; the story continues in the next part."
        )
    return "\n\n".join(parts)


def _gemini_generate_part(client, model: str, system_prompt: str, pdf_bytes: bytes,
                           max_attempts: int = 5):
    """Generates the narration for one PDF part. A generator so a rate-limit
    retry can surface a live log line instead of the connection going quiet
    for up to a minute; yields {type:"log"} frames along the way and exactly
    one final {type:"part_done", text} or {type:"part_error", message}."""
    from google.genai import types
    from google.genai import errors as genai_errors

    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")],
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt, temperature=0.7,
                    max_output_tokens=8192),
            )
            text = (resp.text or "").strip()
            if not text:
                raise RuntimeError("Gemini returned an empty response")
            yield {"type": "part_done", "text": text}
            return
        except genai_errors.APIError as e:
            code = getattr(e, "code", None)
            msg = getattr(e, "message", None) or str(e)
            if code == 429 and attempt < max_attempts:
                wait = min(60, 5 * (2 ** (attempt - 1)))
                yield {"type": "log", "line": f"  rate-limited - retrying in {wait}s "
                                              f"(attempt {attempt}/{max_attempts})…"}
                time.sleep(wait)
                continue
            yield {"type": "part_error", "message": f"Gemini API error {code}: {msg}"}
            return
        except Exception as e:  # noqa: BLE001 - network hiccups etc: retry a few times too
            if attempt < max_attempts:
                wait = 3 * attempt
                yield {"type": "log", "line": f"  {type(e).__name__} - retrying in {wait}s…"}
                time.sleep(wait)
                continue
            yield {"type": "part_error", "message": f"{type(e).__name__}: {e}"}
            return


def _dedupe_overlap(prev_text: str, next_text: str, window: int = 700, min_match: int = 30) -> str:
    """The PDF parts share a few overlap pages for continuity (see PART_OVERLAP)
    - the prompt asks Gemini not to re-narrate them, but models don't always
    comply. Finds the longest matching block between the tail of `prev_text`
    and the head of `next_text`; if it's substantial, drops everything in
    `next_text` up to the end of that match (extended to the next whitespace
    so it doesn't cut mid-sentence). A no-op when there's no real duplication
    - the common case when the model followed the instruction."""
    a = prev_text[-window:]
    b = next_text[:window]
    match = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    if match.size < min_match:
        return next_text
    cut = match.b + match.size
    rest = next_text[cut:]
    return rest.lstrip()


def _stitch_script_parts(texts: list[str]) -> str:
    if not texts:
        return ""
    out = [texts[0].strip()]
    for t in texts[1:]:
        out.append(_dedupe_overlap(out[-1], t.strip()))
    return "\n\n".join(p for p in out if p)


def _build_gemini_script_stream(chapter_id: str, gemini_settings: dict):
    parts_dir = _gemini_parts_dir(chapter_id)
    part_files = sorted(parts_dir.glob("*.pdf"))
    if not part_files:
        yield {"type": "error", "message": "No Gemini parts found for this chapter - "
                                            "download the chapter first (parts are "
                                            "created automatically)."}
        return
    try:
        client = _gemini_client()
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "message": str(e)}
        return

    model = gemini_settings.get("model") or GEMINI_DEFAULT_MODEL
    if model not in GEMINI_MODELS:
        model = GEMINI_DEFAULT_MODEL
    tone = gemini_settings["tone"]
    narration_type = gemini_settings.get("narration_type") or "first_person"
    style_reference = gemini_settings.get("style_reference") or ""

    n = len(part_files)
    min_interval = _gemini_min_interval(model)
    texts: list[str] = []
    last_call = 0.0
    yield {"type": "start", "of_parts": n, "message": f"Generating script with {model} - "
                                                       f"{n} part(s)…"}
    for i, path in enumerate(part_files, start=1):
        yield {"type": "log", "line": f"Generating part {i} of {n}…"}
        yield {"type": "progress", "part": i, "of_parts": n}
        if i > 1:
            wait = min_interval - (time.monotonic() - last_call)
            if wait > 0:
                time.sleep(wait)
        system_prompt = _gemini_system_prompt(tone, narration_type, style_reference,
                                               i == 1, i == n)
        pdf_bytes = path.read_bytes()
        text = None
        for ev in _gemini_generate_part(client, model, system_prompt, pdf_bytes):
            if ev["type"] == "part_done":
                text = ev["text"]
            elif ev["type"] == "part_error":
                yield {"type": "error", "part": i, "of_parts": n,
                       "message": f"Part {i} of {n} failed: {ev['message']} - fix the "
                                  f"issue above and click Generate again to retry."}
                return
            else:
                yield ev
        last_call = time.monotonic()
        texts.append(text or "")
        yield {"type": "log", "line": f"  part {i} of {n} done ({len(text or '')} chars)."}

    script = _stitch_script_parts(texts)
    yield {"type": "result", "script": script}


@app.get("/gemini_script_stream")
def gemini_script_stream():
    """Generate the narration script with the Gemini API from a chapter's
    already-prepared gemini_parts/*.pdf files (see /gemini_parts_stream -
    this route never creates or re-splits them, only reads what's there).

    EventSource (GET) only, params: ?chapter_id=...&project=<slug>&ch_no=<n>.
    Tone/narration-type/style-reference/model come from that PROJECT's saved
    settings (Settings screen), not query params - keeps the pasted style
    reference out of a URL entirely. Emits {type:start|log|progress|done|error}
    exactly like /gemini_parts_stream; `done` carries {script: "<full text>"}."""
    chapter_id = _sanitize_chapter(request.args.get("chapter_id", ""))
    slug, ch_no = _scope_params()
    work_dir = projects.chapter_work_dir(slug, ch_no) if slug and ch_no else chapter_work_dir(chapter_id)
    output_dir = projects.chapter_output_dir(slug, ch_no) if slug and ch_no else chapter_output_dir(chapter_id)

    gemini_settings: dict = {"tone": tones.DEFAULT_TONE}
    if slug and projects.exists(slug):
        project = projects.load(slug)
        gemini_settings = {**project["settings"]["gemini"], "tone": projects.chapter_tone(project, ch_no)}

    def stream():
        try:
            with config.chapter_scope(work_dir, output_dir):
                if not chapter_id:
                    yield _sse({"type": "error", "message": "chapter_id required."})
                    return
                for item in _build_gemini_script_stream(chapter_id, gemini_settings):
                    if item["type"] == "result":
                        yield _sse({"type": "done", "ok": True, "script": item["script"]})
                    else:
                        yield _sse(item)
        except GeneratorExit:
            raise
        except Exception as e:  # noqa: BLE001 - last-resort: still report, don't crash the stream
            yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


# --------------------------------------------------------------------------- #
# Gemini API key - the ONE credential in this app that is genuinely global
# (not per-project, unlike tone/style/model above), and NEVER in project.json.
#
# Preferred storage is the OS credential store (Windows Credential Manager,
# via the `keyring` package) instead of a plaintext .env line - this is
# obfuscation, not absolute secrecy: a local app's key is always ultimately
# readable on the machine it runs on, one way or another, by whoever controls
# that machine. The real protection was always that it's each user's OWN key
# and never leaves their machine - keyring just keeps it out of a plaintext
# file that's one accidental "open in Notepad" or careless screen-share away
# from being read. Falls back to the existing .env convention (same as
# GROQ_API_KEY/HF_TOKEN elsewhere in this codebase) when keyring has no usable
# backend - still local-only, just plaintext again.
#
# A key already sitting in .env from before this existed is migrated into
# keyring (and removed from .env) the first time it's read, so installs that
# predate this also get the improvement, not just new keys saved from now on.
# --------------------------------------------------------------------------- #
_KEYRING_SERVICE = "ReCapper"
_KEYRING_ACCOUNT = "GEMINI_API_KEY"


def _keyring_backend():
    try:
        import keyring
        keyring.get_keyring()  # raises/returns a no-op fail backend if none is usable
        return keyring
    except Exception:  # noqa: BLE001 - keyring missing or no OS backend -> caller falls back to .env
        return None


def _env_file_path() -> Path:
    return ROOT / ".env"


def _read_env_dict() -> dict:
    path = _env_file_path()
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        out[k.strip()] = v.strip()
    return out


def _set_env_value(key: str, value: str | None) -> None:
    """Set (or, when `value` is falsy, remove) one KEY=value line in the
    project-root .env file, preserving every other line untouched - a
    read-modify-write version of what setup.bat does with a one-shot
    append for HF_TOKEN, since this can also be called to update/clear an
    already-set key from the running app, not just write it once."""
    path = _env_file_path()
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
    out, found = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            found = True
            if value:
                out.append(f"{key}={value}")
            continue  # drop the line entirely when clearing
        out.append(line)
    if value and not found:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")


def _get_gemini_key() -> tuple[str, str]:
    """Returns (key, source); source is "keyring", "env", or "" (unset)."""
    kr = _keyring_backend()
    env_key = _read_env_dict().get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    if not kr:
        return env_key, ("env" if env_key else "")
    try:
        key = kr.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
    except Exception:  # noqa: BLE001
        key = None
    if key:
        return key, "keyring"
    if env_key:  # one-time migration off plaintext storage
        try:
            kr.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, env_key)
            _set_env_value("GEMINI_API_KEY", None)
            return env_key, "keyring"
        except Exception:  # noqa: BLE001 - migration failed; .env still has it
            pass
    return env_key, ("env" if env_key else "")


def _set_gemini_key(key: str) -> str:
    """Saves `key` and returns where it ended up: "keyring" or "env"."""
    os.environ["GEMINI_API_KEY"] = key  # effective immediately, no restart needed
    kr = _keyring_backend()
    if kr:
        try:
            kr.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, key)
            _set_env_value("GEMINI_API_KEY", None)  # don't leave a stale plaintext copy
            return "keyring"
        except Exception:  # noqa: BLE001
            pass
    _set_env_value("GEMINI_API_KEY", key)
    return "env"


def _clear_gemini_key() -> None:
    os.environ.pop("GEMINI_API_KEY", None)
    kr = _keyring_backend()
    if kr:
        try:
            kr.delete_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
        except Exception:  # noqa: BLE001
            pass
    _set_env_value("GEMINI_API_KEY", None)


@app.get("/api/gemini/key_status")
def api_gemini_key_status():
    key, source = _get_gemini_key()
    return jsonify(set=bool(key), hint=(f"…{key[-4:]}" if len(key) >= 4 else ""), source=source)


@app.post("/api/gemini/key")
def api_set_gemini_key():
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify(error="Key required."), 400
    source = _set_gemini_key(key)
    return jsonify(ok=True, source=source)


@app.delete("/api/gemini/key")
def api_clear_gemini_key():
    _clear_gemini_key()
    return jsonify(ok=True)


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
        pdf_path = _ensure_dir(_framer_dir(chapter_id)) / "_upload.pdf"
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
        payload = _stitch_chapter(pdf_path, chapter_id)
    except Exception as e:  # noqa: BLE001 - report stitch failures to the UI
        return jsonify(error=f"Stitch failed: {type(e).__name__}: {e}"), 500
    return jsonify(payload)


def _stitch_chapter(pdf_path: Path, chapter_id: str) -> dict:
    """Stitch a PDF into the chapter's one strip; returns the preview payload."""
    payload = _make_preview(_stitch_pdf(pdf_path, chapter_id), chapter_id)
    payload["source_pdf"] = str(pdf_path)
    return payload


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
_PACE_TAG_RE = re.compile(r'^\s*\[(' + "|".join(pacing.PACES) + r')\]\s*', re.IGNORECASE)


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


def split_script_into_lines(text: str) -> list[tuple[str, str | None]]:
    """Turn pasted narration prose into short, boxable segments (see module
    note above), each with the delivery tag (pacing.PACES) its paragraph started
    with, or None. Paragraph order and sentence order are both preserved."""
    lines: list[tuple[str, str | None]] = []
    for para in re.split(r'\n\s*\n', text):
        tag = _PACE_TAG_RE.match(para)
        pace = tag.group(1).lower() if tag else None
        blob = _normalize_ws(para[tag.end():] if tag else para)
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
                    lines.append((cur, pace))
                    cur = piece
                else:
                    cur = cur + " " + piece
            if len(pieces) > 1:
                # A sentence long enough to need clause-splitting: never let
                # its trailing clause fragment blend into the NEXT sentence's
                # segment (e.g. a short "Finally," clause gluing onto the
                # sentence before it) - flush now, keep fragments confined
                # to the sentence they came from.
                lines.append((cur, pace))
                cur = ""
        if cur:
            lines.append((cur, pace))
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
    return jsonify(lines=[text for text, _ in lines], paces=[pace for _, pace in lines])


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
    A trailing "o" ("mo", "go") makes the region an ellipse inscribed in its box instead
    of the full rectangle. Shared by BOTH the export and the live preview so they look identical.
    """
    w, h = rx - lx, ry - ly
    if w <= 0 or h <= 0:
        return
    oval = style.endswith("o")
    style = style.rstrip("o")
    minside = min(w, h)
    patch = img.crop((lx, ly, rx, ry))
    if style == "g":
        blurred = patch.filter(ImageFilter.GaussianBlur(max(8.0, BLUR_STRENGTH * minside)))
    else:  # mosaic / pixelate (default)
        block = max(3, int(round(BLUR_STRENGTH * minside)))
        sw, sh = max(1, w // block), max(1, h // block)
        blurred = patch.resize((sw, sh), Image.BILINEAR).resize((w, h), Image.NEAREST)
    if oval:
        # anti-aliased ellipse: drawn at 4x, shrunk back
        mask = Image.new("L", (w * 4, h * 4), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, w * 4 - 1, h * 4 - 1), fill=255)
        blurred = Image.composite(blurred, patch, mask.resize((w, h), Image.LANCZOS))
    img.paste(blurred, (lx, ly))


def _crop_frame(strip: Image.Image, entry: dict, out_path: Path, blur: bool) -> None:
    """Crop one frame from the full-res strip, then flip it and, when `blur` is on, blur its regions.

    `entry` carries this frame's bbox + blur regions in TRUE strip pixels and a
    flip flag. Blur regions are translated into crop-local coords and blurred in
    place; the flip is applied last so on-screen left/right matches what the user toggled.
    """
    x, y, w, h = (int(round(v)) for v in entry["bbox"])
    x = max(0, min(x, strip.width - 1))
    y = max(0, min(y, strip.height - 1))
    w = max(1, min(w, strip.width - x))
    h = max(1, min(h, strip.height - y))
    crop = strip.crop((x, y, x + w, y + h))

    for region in (entry.get("blur") or []) if blur else []:
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
        return jsonify(_export_chapter(_sanitize_chapter(data.get("chapter_id") or ""),
                                       data.get("entries") or [], *_scope_params()))
    except Refused as e:
        return jsonify(error=str(e)), e.status


def _export_chapter(chapter_id: str, entries: list[dict], slug: str | None, ch_no: str | None) -> dict:
    """Render every frame of `entries` and write the chapter's mapping.json and manifest."""
    if not entries:
        raise Refused("No entries to export.")
    if not any(f.get("bbox") for entry in entries for f in entry.get("frames") or []):
        raise Refused("No line had a frame bound. Draw a box first.")

    strip_path = _strip_path(chapter_id)
    if not strip_path.exists():
        raise Refused(f"No strip for {chapter_id}; load a PDF first.")
    strip = Image.open(strip_path).convert("RGB")
    blur = bool(slug) and projects.load(slug)["settings"]["framing"]["blur_text"]

    # The old frames and the narration voiced for the old lines go before the new frames are written.
    frames_dir = chapter_work_dir(chapter_id) / "frames"
    clear_directory(frames_dir)
    clear_directory(chapter_work_dir(chapter_id) / "audio")

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
        pace = entry.get("pace") if entry.get("pace") in pacing.PACES else None

        out_frames = []
        for j, fr in enumerate(frames):
            frame_name = f"frame_{i:03d}{_frame_suffix(j)}.png"
            frame_path = frames_dir / frame_name
            _crop_frame(strip, fr, frame_path, blur)
            total_frames += 1
            out_frames.append({
                "file": frame_name,
                "image": str(frame_path),
                "flip": bool(fr.get("flip")),
                "blur_regions": [list(_region_box_style(r)) for r in (fr.get("blur") or [])] if blur else [],
                "bbox": [int(round(v)) for v in fr["bbox"]],
            })

        mapping.append({
            "order": i,
            "text": line_text,
            "duration_s": duration_s,
            "pace": pace,
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
            emotion=pace,
            duration_s=duration_s,
            selected=True,
            has_person=True,
            has_face=True,
            is_text_only=False,
        ))

    framer_dir = _ensure_dir(_framer_dir(chapter_id))
    mapping_path = framer_dir / "mapping.json"
    # Atomic + backed up (see atomic_io.py) - this is the finished boxing/script
    # work (line -> frame bindings), the exact thing that must never be lost
    # to a half-written file.
    atomic_write_json(mapping_path, {"chapter_id": chapter_id, "lines": mapping})

    # A manifest the orchestrator can resume from. stage="clean" means the next
    # stages to run are audio (s6) then video (s7); everything before is skipped.
    m = Manifest(
        chapter_id=chapter_id,
        source_pdf=str(framer_dir / "_upload.pdf"),
        work_dir=str(chapter_work_dir(chapter_id)),
        doc_type="webtoon",
        stage="clean",
        pages=[Page(index=1, image=str(strip_path), panels=_manifest_panels(chapter_id))],
        beats=beats,
    )
    m.save()

    if slug and ch_no:
        projects.set_status(slug, ch_no, "boxed")
        projects.write_media_snapshot(slug, ch_no)

    return {
        "chapter_id": chapter_id,
        "lines_written": len(beats),
        "frames_written": total_frames,
        "frames_dir": str(frames_dir),
        "mapping_path": str(mapping_path),
        "manifest_path": str(m.path),
        "next_command": f"python orchestrator.py --chapter {chapter_id} --from audio",
    }


# --------------------------------------------------------------------------- #
# generate audio + video - a proper job QUEUE, not a request/SSE-stream-shaped
# job. Rationale (this replaced an earlier design where the SSE connection
# itself WAS the job - a request-scoped generator running the subprocess and
# yielding its stdout): a job's real state has to survive the browser
# navigating away and back, so it can't live only inside one HTTP connection.
# A Job is a plain object in the _JOBS registry, run by ONE background worker
# thread pulled off _QUEUE_ORDER in order (rendering is GPU-bound - see
# s6_tts.py - so jobs run one at a time, never in parallel, on purpose).
# Any route can ask _JOBS for a job's CURRENT status at any time; the
# frontend polls GET /api/jobs (global, for the corner widget + queue) and
# GET /api/jobs/<id> (per-chapter detail) instead of trusting a live stream -
# so "navigate away and back" always reflects the real state, never a stale
# assumption.
# --------------------------------------------------------------------------- #
_JOBS: dict[str, "Job"] = {}
_QUEUE_ORDER: list[str] = []          # queued job ids, in run order
_ACTIVE_CHAPTERS: set[str] = set()    # chapter_ids with a queued OR running job
_JOBS_LOCK = threading.Lock()
_JOBS_COND = threading.Condition(_JOBS_LOCK)

# Rolling per-`what` history of (seconds per beat) from completed jobs, used
# to estimate remaining time for running/queued jobs - see _rate_for(). Seed
# values are a rough guess (TTS dominates "both"/"voice"; "video" is mostly
# ffmpeg) used only until real history exists; capped so old outliers age out.
_RENDER_HISTORY: dict[str, list[float]] = {"both": [9.0], "voice": [7.0], "video": [2.5]}
_RENDER_HISTORY_CAP = 20
# Jobs whose length isn't per-beat: a branded join is a copy once the clips are
# cached; a script is two Claude calls over a whole chapter (a rough guess).
FIXED_ESTIMATE_S = {"brand": 15.0, "script": 600.0}


class Job:
    """One audio/video generate run. Deliberately NOT tied to any request or
    SSE connection - see module note above."""

    def __init__(self, chapter_id: str, what: str, slug: str | None, ch_no: str | None,
                 total_beats: int, target: str | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.chapter_id = chapter_id
        self.what = what                  # both|voice|video|brand|script
        self.slug = slug
        self.ch_no = ch_no
        self.target = target              # brand: the merged video being branded
        self.fraction = 0.0               # brand/script: progress reported by the job itself
        self.resume_at: str | None = None  # script: set while paused on Claude's usage limit
        self.status = "queued"            # queued|running|done|failed|cancelled
        self.total_beats = total_beats
        self.audio = 0
        self.video = 0
        self.message = ""
        # True while stages/s6_tts.py is fetching a not-yet-cached Chatterbox
        # model (see its _ensure_model_cached) - lets the UI show real, live
        # status instead of a progress bar stuck at 0% (audio/video counters
        # can't move yet since synthesis hasn't started). See "[voice-model]"
        # handling below.
        self.downloading_model = False
        self.video_url: str | None = None
        self.ok: bool | None = None
        self.log: list[str] = []
        self.created_at = _now_iso()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.proc: subprocess.Popen | None = None
        self.cancel_requested = False
        self.claude: dict | None = None    # script: live pass / panel / token stats
        self.before_claude_call: Callable[[], None] | None = None   # script: a batch's usage-limit gate
        self.mirror: Callable[[str], None] | None = None   # a batch's per-chapter log
        self.last_activity = time.time()   # when the job last logged or emitted anything
        self.render = 0.0                  # video: share of the frames encoded, read from ffmpeg / MoviePy output
        self.events: list[dict] = []       # script: pushed to the browser by /api/jobs/<id>/events
        self._events_cond = threading.Condition()

    def append_log(self, line: str) -> None:
        self.last_activity = time.time()
        self.log.append(line)
        if len(self.log) > 500:
            self.log.pop(0)
        if self.mirror:
            self.mirror(line)

    def emit(self, event: dict) -> None:
        self.last_activity = time.time()
        with self._events_cond:
            self.events.append(event)
            self._events_cond.notify_all()

    def note(self, line: str) -> None:
        self.append_log(line)
        self.emit({"type": "log", "line": line})

    def progress_fraction(self) -> float:
        """Mirrors the old client-side estimate, now computed server-side so
        ANY poller (corner widget, chapter detail, queue ETA) sees the same
        number: s6 prints one "[NNN] …" per voiced line, s7 one "line N: …"
        per rendered line. Audio is the slow part, weighted heavier for a
        combined run."""
        if self.what in FIXED_ESTIMATE_S:
            return self.fraction
        if self.total_beats <= 0 or (self.audio == 0 and self.video == 0 and self.render == 0):
            return 0.0
        a = min(1.0, self.audio / self.total_beats)
        v = min(1.0, self.video / self.total_beats)
        if self.what == "video":
            return min(1.0, 0.05 * v + 0.90 * self.render)
        if self.what == "voice":
            return min(1.0, 0.05 + 0.90 * a)
        return min(1.0, 0.05 + 0.70 * a + 0.03 * v + 0.22 * self.render)

    def estimated_total_s(self) -> float:
        if self.what in FIXED_ESTIMATE_S:
            return FIXED_ESTIMATE_S[self.what]
        return max(1.0, self.total_beats) * _rate_for(self.what)

    def eta_s(self, queued_ahead_s: float = 0.0) -> float | None:
        """Seconds until THIS job finishes: remaining run time, plus (for a
        queued job) however long everything ahead of it in line is estimated
        to still take - see _jobs_with_eta()."""
        if self.status == "running" and self.what == "script":
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(self.started_at)).total_seconds()
            done = self.progress_fraction()
            return elapsed * (1.0 - done) / done if done > 0.05 else None
        if self.status == "running":
            return max(0.0, self.estimated_total_s() * (1.0 - self.progress_fraction()))
        if self.status == "queued":
            return queued_ahead_s + self.estimated_total_s()
        return None

    def public(self) -> dict:
        return {
            "id": self.id, "chapter_id": self.chapter_id, "what": self.what,
            "project": self.slug, "ch_no": self.ch_no, "target": self.target,
            "status": self.status,
            "total_beats": self.total_beats, "audio": self.audio, "video": self.video,
            "progress": round(self.progress_fraction(), 4) if self.status == "running" else
                        (1.0 if self.status == "done" else 0.0),
            "message": self.message, "video_url": self.video_url, "ok": self.ok,
            "resume_at": self.resume_at, "claude": self.claude,
            "downloading_model": self.downloading_model,
            "log": list(self.log), "created_at": self.created_at,
            "started_at": self.started_at, "finished_at": self.finished_at,
        }


def _rate_for(what: str) -> float:
    hist = _RENDER_HISTORY.get(what) or _RENDER_HISTORY["both"]
    return sum(hist) / len(hist)


def _record_history(job: "Job") -> None:
    if not (job.started_at and job.finished_at) or job.total_beats <= 0:
        return
    try:
        started = datetime.fromisoformat(job.started_at)
        finished = datetime.fromisoformat(job.finished_at)
        secs_per_beat = max(0.1, (finished - started).total_seconds() / job.total_beats)
    except Exception:  # noqa: BLE001 - a bad timestamp must not crash the worker
        return
    hist = _RENDER_HISTORY.setdefault(job.what, [])
    hist.append(secs_per_beat)
    del hist[:-_RENDER_HISTORY_CAP]


def _jobs_with_eta() -> list[dict]:
    """All queued + running jobs, each with `eta_s` filled in, in the order
    they'll actually run (running job first, if any, then queue order) - the
    queue-position math (each job's wait = everything ahead of it) only makes
    sense computed together like this, not per-job in isolation."""
    with _JOBS_LOCK:
        running = [j for j in _JOBS.values() if j.status == "running"]
        queued = [_JOBS[jid] for jid in _QUEUE_ORDER if jid in _JOBS]
    ordered = running + queued
    out = []
    ahead_s = 0.0
    for j in ordered:
        d = j.public()
        d["eta_s"] = j.eta_s(ahead_s)
        out.append(d)
        ahead_s = (d["eta_s"] if j.status == "running" else
                   (ahead_s + j.estimated_total_s()))
    return out


def _job_worker() -> None:
    while True:
        with _JOBS_COND:
            while not _QUEUE_ORDER:
                _JOBS_COND.wait()
            job_id = _QUEUE_ORDER.pop(0)
            job = _JOBS.get(job_id)
        if job is None:
            continue
        if job.status == "cancelled":            # cancelled while still queued
            with _JOBS_LOCK:
                _ACTIVE_CHAPTERS.discard(job.chapter_id)
            continue
        _run_job(job)


threading.Thread(target=_job_worker, daemon=True).start()


# Hardware that must never serve two jobs at once, whether they come from the queue or from
# a batch's lanes: each job holds the locks for the resources it uses while it runs.
_HARDWARE = {name: threading.Lock() for name in ("claude", "audio", "video")}
_HARDWARE_USED = {"script": ("claude",), "voice": ("audio",), "video": ("video",),
                  "both": ("audio", "video"), "brand": ("video",)}


def _run_job(job: "Job") -> None:
    with contextlib.ExitStack() as held:
        for name in _HARDWARE_USED[job.what]:
            held.enter_context(_HARDWARE[name])
        job.status = "running"
        job.started_at = _now_iso()
        try:
            {"brand": _run_brand_job, "script": _run_script_job}.get(job.what, _run_pipeline_job)(job)
        finally:
            job.finished_at = _now_iso()
            job.emit({"type": "end"})
            _record_history(job)
            with _JOBS_LOCK:
                _ACTIVE_CHAPTERS.discard(job.chapter_id)


def _run_now(job: "Job") -> None:
    """Run `job` on the calling thread (a batch lane) instead of the queue's worker."""
    with _JOBS_LOCK:
        _JOBS[job.id] = job
        _ACTIVE_CHAPTERS.add(job.chapter_id)
    _run_job(job)


_FFMPEG_PROGRESS_LINE = re.compile(r"^\w+=\s*\S*$")


def _ffmpeg_runner(job: "Job") -> branding.Runner:
    """Runs one ffmpeg command for `job`: streams `-progress` output into the
    job's progress fraction and its log, and lets cancel terminate the process."""
    def run(cmd, duration, on_fraction):
        job.append_log("$ " + " ".join(cmd))
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, bufsize=1, encoding="utf-8",
            errors="replace", **NO_CONSOLE)
        job.proc = proc
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                done_us = line.partition("=")[2]
                if duration > 0 and done_us.isdigit():
                    on_fraction(min(1.0, int(done_us) / 1e6 / duration))
            elif line and not _FFMPEG_PROGRESS_LINE.match(line):
                job.append_log(line)
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")
    return run


def _run_brand_job(job: "Job") -> None:
    def on_fraction(f: float) -> None:
        job.fraction = f

    try:
        final = branding.render_final(job.slug, job.target, _ffmpeg_runner(job), on_fraction)
        job.status, job.ok, job.message = "done", True, "Done."
        job.fraction = 1.0
        job.video_url = f"/projects/{job.slug}/merged/{final.name}?v={int(final.stat().st_mtime)}"
    except Exception as e:  # noqa: BLE001 - report, don't crash the worker
        if job.cancel_requested:
            job.status, job.ok, job.message = "cancelled", False, "Stopped by user."
        else:
            job.status, job.ok = "failed", False
            job.message = f"{type(e).__name__}: {e}"
            job.append_log(f"ERROR: {job.message}")


_FFMPEG_TIME = re.compile(r"\btime=(\d+):(\d+):(\d+(?:\.\d+)?)")
_MOVIEPY_FRAMES = re.compile(r"frame_index:.*?(\d+)/(\d+)")


def _render_fraction(line: str, output_dir: Path) -> float | None:
    """How far a render has got, from one line of s7's output: ffmpeg's `time=` progress
    against the planned length, or MoviePy's frame counter. None for any other line."""
    if frames := _MOVIEPY_FRAMES.search(line):
        return int(frames[1]) / max(1, int(frames[2]))
    if clock := _FFMPEG_TIME.search(line):
        try:
            total = json.loads((output_dir / "video_plan.json").read_text(encoding="utf-8"))["total_s"]
        except (OSError, ValueError, KeyError):
            return None
        return (int(clock[1]) * 3600 + int(clock[2]) * 60 + float(clock[3])) / max(1.0, total)
    return None


NATIVE_CRASHES = {0xC0000005: "an access violation", 0xC0000409: "a stack buffer overrun",
                  0xC00000FD: "a stack overflow", 0xC0000142: "a DLL failing to start"}
NATIVE_CRASH_RETRIES = 1


def _crash_message(code: int, log: list[str]) -> str:
    """Explain a pipeline child that died from a native crash (Python had no chance to raise), naming
    the Python line it was on when PYTHONFAULTHANDLER printed one."""
    current = max((i for i, line in enumerate(log) if line.startswith("Current thread")), default=None)
    frames = [] if current is None else [line.strip() for line in log[current:] if line.strip().startswith("File ")]
    where = f" at {frames[0]}" if frames else ""
    return (f"The pipeline crashed with {NATIVE_CRASHES[code]} (Windows exit code 0x{code:X}){where}. This is a "
            f"crash inside a native library, not a Python error; low free memory is a common trigger, so close "
            f"other heavy programs and run it again. The job log has the details.")


def _run_pipeline_job(job: "Job") -> None:
    slug, ch_no, chapter_id = job.slug, job.ch_no, job.chapter_id
    work_dir = projects.chapter_work_dir(slug, ch_no) if slug and ch_no else chapter_work_dir(chapter_id)
    output_dir = projects.chapter_output_dir(slug, ch_no) if slug and ch_no else chapter_output_dir(chapter_id)

    with config.chapter_scope(work_dir, output_dir):
        if slug and ch_no:
            projects.write_media_snapshot(slug, ch_no)   # a settings change must still land

        if job.what == "video":
            from_stage, to_stage = "video", None
        elif job.what == "voice":
            from_stage, to_stage = "audio", "audio"
        else:
            from_stage, to_stage = "audio", None

        # -u + PYTHONUNBUFFERED so each print streams immediately; utf-8 so
        # the child never dies on a unicode arrow. MANHWA_WORK_ROOT/OUTPUT_ROOT
        # hand the same scope to the orchestrator subprocess (config.py's
        # env-var fallback) - this worker thread has no Flask request context
        # for it to inherit.
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONFAULTHANDLER="1")
        if slug and ch_no:
            env["MANHWA_WORK_ROOT"] = str(work_dir)
            env["MANHWA_OUTPUT_ROOT"] = str(output_dir)
        cmd = [sys.executable, "-u", str(ROOT / "orchestrator.py"),
               "--chapter", chapter_id, "--from", from_stage]
        if to_stage:
            cmd += ["--to", to_stage]
        job.append_log("$ " + " ".join(cmd))

        proc = None
        try:
            for attempt in range(1 + NATIVE_CRASH_RETRIES):
                proc = subprocess.Popen(
                    cmd, cwd=str(ROOT), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, text=True, bufsize=1,
                    encoding="utf-8", errors="replace", **NO_CONSOLE)
                job.proc = proc

                for line in proc.stdout:                       # streams as it arrives
                    line = line.rstrip("\n")
                    job.append_log(line)
                    if re.match(r"^\s*\[\d{1,4}\]", line):
                        job.audio += 1
                        job.downloading_model = False
                    elif re.match(r"^\s*line \d+:", line):
                        job.video += 1
                        job.downloading_model = False
                    elif (done := _render_fraction(line, output_dir)) is not None:
                        job.render = max(job.render, done)
                    elif "[voice-model]" in line:
                        # see stages/s6_tts.py._ensure_model_cached - surfaces a
                        # not-yet-cached model download with a live message instead
                        # of a progress bar stuck at 0% (audio/video haven't moved
                        # yet since synthesis hasn't started).
                        job.message = line.strip()
                        job.downloading_model = "ready" not in line
                proc.wait()
                if job.cancel_requested or proc.returncode not in NATIVE_CRASHES or attempt == NATIVE_CRASH_RETRIES:
                    break
                job.append_log(_crash_message(proc.returncode, job.log) + " Trying once more.")
                job.audio = job.video = 0

            if job.cancel_requested:
                job.status, job.ok = "cancelled", False
                job.message = "Stopped by user."
            elif proc.returncode == 0:
                if job.what == "voice":
                    # audio-only: no video ran, so recap.mp4/status are
                    # untouched - a stale render stays valid until "Re-render
                    # video" is run over the new audio.
                    job.ok = True
                    job.message = "Audio regenerated. Re-render video to hear it in the recap."
                else:
                    mp4 = output_dir / "recap.mp4"
                    if mp4.exists() and slug and ch_no:
                        projects.set_status(slug, ch_no, "rendered")
                    job.ok = mp4.exists()
                    job.video_url = _video_url(chapter_id, mp4, slug, ch_no)
                    job.message = "Done." if mp4.exists() else "Finished, but recap.mp4 was not found."
                job.status = "done" if job.ok else "failed"
            else:
                job.status, job.ok = "failed", False
                job.message = (_crash_message(proc.returncode, job.log) if proc.returncode in NATIVE_CRASHES
                               else f"Pipeline exited with code {proc.returncode}.")
        except Exception as e:  # noqa: BLE001 - report, don't crash the worker
            job.status, job.ok = "failed", False
            job.message = f"{type(e).__name__}: {e}"
            job.append_log(f"ERROR: {job.message}")
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass


def _enqueue(job: "Job") -> None:
    with _JOBS_COND:
        _JOBS[job.id] = job
        _QUEUE_ORDER.append(job.id)
        _ACTIVE_CHAPTERS.add(job.chapter_id)
        _JOBS_COND.notify()


@app.post("/api/generate")
def api_generate():
    """Enqueue an audio+video / voice-only / video-only render for a chapter.
    Body: JSON {chapter, what:both|voice|video, project?, ch_no?}. Returns
    immediately with {job_id} - it does NOT run inline or stream; poll
    GET /api/jobs/<job_id> (or GET /api/jobs for the global queue/corner
    widget) for progress. Only one active (queued or running) job per
    chapter at a time; a second request for the SAME chapter is rejected,
    but a DIFFERENT chapter queues behind whatever's already running."""
    data = request.get_json(silent=True) or {}
    what = (data.get("what") or "both").strip().lower()
    try:
        job = _new_render_job(_sanitize_chapter(data.get("chapter") or ""),
                              what if what in ("both", "voice", "video") else "both", *_scope_params())
    except Refused as e:
        return jsonify(error=str(e)), e.status
    _enqueue(job)
    return jsonify(ok=True, job_id=job.id, job=job.public())


def _new_render_job(chapter_id: str, what: str, slug: str | None, ch_no: str | None) -> "Job":
    """A not-yet-started audio+video / voice / video job for a chapter whose frames are exported."""
    work_dir = projects.chapter_work_dir(slug, ch_no) if slug and ch_no else chapter_work_dir(chapter_id)
    with config.chapter_scope(work_dir, projects.chapter_output_dir(slug, ch_no)
                               if slug and ch_no else chapter_output_dir(chapter_id)):
        total = _beat_count(chapter_id)
    if not total:
        raise Refused(f"No narration lines for {chapter_id}. Export from the Framer first.")
    blockers = library.render_blockers(work_dir, what)
    if blockers:
        raise Refused(" ".join(blockers), 409)
    _refuse_if_busy(chapter_id, "A generate job")

    job = Job(chapter_id, what, slug, ch_no, total)
    label = {"both": "audio + video", "voice": "audio (voice only)", "video": "video"}[what]
    job.append_log(f"Queued {label} for {chapter_id} ({total or '?'} line(s))…")
    return job


def _refuse_if_busy(chapter_id: str, what: str) -> None:
    with _JOBS_LOCK:
        if chapter_id in _ACTIVE_CHAPTERS:
            raise Refused(f"{what} is already queued or running for {chapter_id}.", 409)


def _beat_count(chapter_id: str) -> int:
    """How many narration lines (beats) this chapter has, for the progress bar
    and time estimate. Must be called inside the right chapter_scope."""
    try:
        return len(Manifest.load(chapter_work_dir(chapter_id)).beats)
    except Exception:  # noqa: BLE001 - missing/old manifest -> unknown total
        return 0


# --------------------------------------------------------------------------- #
# Claude script engine - the local `claude` CLI instead of the Gemini API (see
# claude_engine.py). A "script" job splits the chapter strip into panels, runs
# the two passes, and leaves the narration blocks in work/<ch>/claude/state.json;
# the framer then opens the chapter with every block boxed to its panels (see
# _state_from_narration). TTS and render consume the framer's export unchanged.
# --------------------------------------------------------------------------- #
def _claude_state_path(chapter_id: str) -> Path:
    return chapter_work_dir(chapter_id) / "claude" / "state.json"


def _manifest_panels(chapter_id: str) -> list[Panel]:
    work = chapter_work_dir(chapter_id)
    if not (work / "manifest.json").exists():
        return []
    pages = Manifest.load(work).pages
    return pages[0].panels if pages else []


def _ensure_panels(chapter_id: str) -> list[Panel]:
    """The chapter's panels, split from its strip (stitching the downloaded PDF
    first if needed) and recorded in the manifest; kept only while they are newer than the strip."""
    panels = _manifest_panels(chapter_id)
    work = chapter_work_dir(chapter_id)
    strip_path = _strip_path(chapter_id)
    if panels and strip_path.exists() and all(
            Path(p.crop).is_file() and Path(p.crop).stat().st_mtime >= strip_path.stat().st_mtime for p in panels):
        return panels
    pdf = _downloaded_pdf(chapter_id)
    if not strip_path.exists():
        if pdf is None:
            raise RuntimeError("Download the chapter first.")
        _make_preview(_stitch_pdf(pdf, chapter_id), chapter_id)
    panels = panel_split.split_panels(strip_path, work / "panels")
    m = (Manifest.load(work) if (work / "manifest.json").exists() else
         Manifest(chapter_id=chapter_id, source_pdf=str(pdf or ""), work_dir=str(work), doc_type="webtoon"))
    m.pages = [Page(index=1, image=str(strip_path), panels=panels)]
    m.save()
    return panels


def _narration_estimate(slug: str, n: int, chars_per_sec: float) -> dict | None:
    """Words and estimated spoken length (seconds, at the project's speaking pace) of chapter
    `n`'s current script, or None when it has none yet."""
    with config.chapter_scope(projects.chapter_work_path(slug, n), projects.chapter_output_path(slug, n)):
        state = _resolve_editor_state(f"{slug}_ch{n}")
    texts = [text for line in (state or {}).get("lines", []) if (text := line.get("text", "").strip())]
    if not texts:
        return None
    return {"words": sum(len(t.split()) for t in texts), "seconds": round(sum(map(len, texts)) / chars_per_sec)}


def _run_script_job(job: "Job") -> None:
    slug, ch_no, chapter_id = job.slug, job.ch_no, job.chapter_id
    read: set[str] = set()
    panel_ids: list[str] = []

    def set_progress(floor: float, span: float, done: int) -> None:
        job.fraction = floor + span * min(1.0, done / job.claude["panels_total"])

    def on_event(event: dict) -> None:
        stats, kind = job.claude, event["type"]
        if kind == "log":
            job.note(event["line"])
            return
        if kind == "plan":
            panel_ids[:] = event["sent"]
            stats["panels_total"] = len(panel_ids)
            job.note(f"Sending {len(panel_ids)} panels; {len(event['dropped'])} blank or uniform panels dropped locally")
        elif kind == "step":
            stats["pass"] = event["pass"]
            job.message = event["label"]
            job.note(event["label"] + "…")
            job.fraction = max(job.fraction, {1: 0.02, 2: 0.60, None: 0.95}[event["pass"]])
        elif kind == "read" and event["panel"] in panel_ids:
            read.add(event["panel"])
            stats["panels_read"] = len(read)
            set_progress(0.02, 0.58, len(read))
        elif kind == "cover" and event["panel"] in panel_ids:
            set_progress(0.60, 0.35, panel_ids.index(event["panel"]) + 1)
        elif kind == "usage":
            stats[f"pass{event['pass']}"] = event["usage"]
        stats["current"] = event.get("panel", stats["current"])
        job.emit(event)

    def on_limit(resume_at: datetime) -> None:
        job.resume_at = resume_at.isoformat(timespec="seconds")
        job.message = f"Usage limit reached - resumes at {resume_at:%H:%M}"
        job.note(job.message)
        while datetime.now().astimezone() < resume_at:
            if job.cancel_requested:
                raise RuntimeError("Stopped by user.")
            time.sleep(1)
        job.resume_at = None
        job.note("Usage limit reset - resuming.")

    def on_proc(proc: subprocess.Popen) -> None:
        job.proc = proc

    try:
        with config.chapter_scope(projects.chapter_work_dir(slug, ch_no), projects.chapter_output_dir(slug, ch_no)):
            settings = projects.load(slug)["settings"]
            job.message = "Splitting the strip into panels"
            job.fraction = 0.02
            split_started = time.monotonic()
            panels = _ensure_panels(chapter_id)
            job.note(f"{len(panels)} panels ready ({time.monotonic() - split_started:.0f}s)")
            job.claude = {"pass": None, "panels_read": 0, "panels_total": len(panels), "current": None,
                          "pass1": None, "pass2": None, "total": None}
            voice = settings["gemini"]
            tone = projects.chapter_tone(projects.load(slug), ch_no)
            job.note(f"Tone: {tones.TONES[tone]['label']}")
            style = [_narration_voice(tone, voice["narration_type"])]
            if voice["style_reference"].strip():
                style.append(_style_reference_directive(voice["style_reference"]))
            state_path = _claude_state_path(chapter_id)
            _ensure_dir(state_path.parent)
            rate = totals.speech_rate(slug)
            job.note(f"Speaking rate: {rate:.1f} chars/s, measured from this project's generated audio" if rate else
                     f"No generated audio in this project yet; assuming {claude_engine.DEFAULT_CHARS_PER_SEC} chars/s")
            narration = claude_engine.run_chapter(
                panels=panels,
                work_dir=state_path.parent,
                state_path=state_path,
                memory_path=projects.story_memory_path(slug),
                ch_no=int(ch_no),
                brief=settings["claude"]["series_brief"],
                style="\n\n".join(style),
                models={"comprehension": settings["claude"]["comprehension_model"],
                        "narration": settings["claude"]["narration_model"]},
                min_frame_s=settings["claude"]["min_frame_s"],
                chars_per_sec=rate or claude_engine.DEFAULT_CHARS_PER_SEC,
                on_event=on_event, on_limit=on_limit, on_proc=on_proc,
                before_call=job.before_claude_call)
            total = claude_engine.read_usage(state_path)["total"]
        job.claude["total"] = total
        job.emit({"type": "total", "usage": total})
        job.note(f"Chapter total: {total['input']:,} in, {total['output']:,} out, {total['cache_read']:,} cache read, "
                 f"{total['cache_write']:,} cache write; estimated API cost ${total['cost_usd']:.2f} "
                 "(not charged on subscription)")
        blocks = narration["blocks"]
        job.status, job.ok, job.fraction = "done", True, 1.0
        job.message = f"Narration ready - {len(blocks)} block(s), about {sum(b['duration_s'] for b in blocks) / 60:.1f} min."
    except Exception as e:  # noqa: BLE001 - report, don't crash the worker
        if job.cancel_requested:
            job.status, job.ok, job.message = "cancelled", False, "Stopped by user."
        else:
            job.status, job.ok = "failed", False
            job.message = f"{type(e).__name__}: {e}"
            job.note(f"ERROR: {job.message}")
    finally:
        job.resume_at = None


def _state_from_narration(chapter_id: str) -> dict | None:
    """Editor lines for a chapter's Claude narration: one line per block, with the
    block's frames cropped and flopped as the engine decided."""
    narration = claude_engine.read_narration(_claude_state_path(chapter_id))
    if not narration:
        return None
    boxes = {p.id: p.bbox for p in _manifest_panels(chapter_id)}

    def frames_of(block: dict) -> list[dict]:
        if "frames" in block:
            return [{"bbox": f["bbox"], "flip": f["flip"]} for f in block["frames"]]
        return [{"bbox": boxes[i], "flip": False} for i in block["panel_ids"] if i in boxes]

    return {
        "chapter_id": chapter_id,
        "saved_at": narration["saved_at"],
        "lines": [{"text": block["narration"], "duration_s": block["duration_s"],
                   "mode": block.get("mode", "sequence"), "pace": block.get("pace"), "frames": frames_of(block)}
                  for block in narration["blocks"]],
    }


@app.get("/api/jobs/<job_id>/events")
def api_job_events(job_id):
    """Server-sent stream of a script job's events (see Job.emit), replayed from
    Last-Event-ID so a reconnect or a reload picks up where it left off."""
    job = _JOBS.get(job_id)
    if job is None:
        return jsonify(error="job not found"), 404
    next_id = int(request.headers.get("Last-Event-ID", -1)) + 1

    def stream():
        nonlocal next_id
        while True:
            with job._events_cond:
                if next_id >= len(job.events):
                    job._events_cond.wait(timeout=15)
                pending = job.events[next_id:]
            if not pending:
                yield ": keepalive\n\n"
            for event in pending:
                yield f"id: {next_id}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                next_id += 1
                if event["type"] == "end":
                    return

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/claude/panels")
def api_claude_panels():
    """The chapter's panel ids and strip boxes, for the strip viewer's read marks."""
    chapter_id = _sanitize_chapter(request.args.get("chapter", ""))
    return jsonify(panels=[{"id": p.id, "bbox": p.bbox} for p in _manifest_panels(chapter_id)])


@app.get("/api/claude/status")
def api_claude_status():
    return jsonify(claude_cli.cli_status())


@app.post("/api/script/claude")
def api_script_claude():
    """Enqueue Claude script generation for a project chapter. Body: JSON
    {chapter, project, ch_no}. Returns {job_id}; poll /api/jobs for progress."""
    try:
        job = _new_script_job(_sanitize_chapter((request.get_json(silent=True) or {}).get("chapter") or ""),
                              *_scope_params())
    except Refused as e:
        return jsonify(error=str(e)), e.status
    _enqueue(job)
    return jsonify(ok=True, job_id=job.id, job=job.public())


def _new_script_job(chapter_id: str, slug: str | None, ch_no: str | None) -> "Job":
    if not (slug and ch_no and projects.exists(slug)):
        raise Refused("Open a project chapter first.")
    status = claude_cli.cli_status()
    if not status["ready"]:
        raise Refused(status["message"], 409)
    if not (_strip_path(chapter_id).exists() or _downloaded_pdf(chapter_id)):
        raise Refused("Download the chapter first.")
    _refuse_if_busy(chapter_id, "A job")
    job = Job(chapter_id, "script", slug, ch_no, 0)
    job.append_log(f"Queued Claude script for {chapter_id}…")
    return job


@app.get("/api/jobs")
def api_jobs():
    """The full job picture: queued + running (with eta_s, in run order) plus
    a short tail of recently finished jobs. Polled by the global corner
    widget (any screen) and by a chapter editor on open, to reflect the REAL
    state instead of assuming nothing changed while the user was elsewhere."""
    active = _jobs_with_eta()
    with _JOBS_LOCK:
        finished = sorted(
            (j for j in _JOBS.values() if j.status in ("done", "failed", "cancelled")),
            key=lambda j: j.finished_at or "", reverse=True)[:10]
        finished = [j.public() for j in finished]
    return jsonify(jobs=active + finished)


@app.get("/api/jobs/<job_id>")
def api_job_detail(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return jsonify(error="job not found"), 404
        d = job.public()
        if job.status in ("running", "queued"):
            ahead_s = 0.0
            if job.status == "queued":
                for jid in _QUEUE_ORDER:
                    if jid == job_id:
                        break
                    j2 = _JOBS.get(jid)
                    if j2:
                        ahead_s += j2.estimated_total_s()
                running = next((j for j in _JOBS.values() if j.status == "running"), None)
                if running:
                    ahead_s += running.eta_s() or 0.0
            d["eta_s"] = job.eta_s(ahead_s)
    return jsonify(job=d)


@app.post("/api/jobs/<job_id>/cancel")
def api_job_cancel(job_id):
    """Cancel a queued job (removed before it ever runs) or a running one
    (its subprocess is terminated directly - immediate, not waiting for the
    next log line like the old SSE-disconnect-triggered cancel did)."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return jsonify(error="job not found"), 404
        if job.status == "queued":
            job.status, job.ok = "cancelled", False
            job.message = "Cancelled before it started."
            job.finished_at = _now_iso()
            try:
                _QUEUE_ORDER.remove(job_id)
            except ValueError:
                pass
            _ACTIVE_CHAPTERS.discard(job.chapter_id)
            return jsonify(ok=True, job=job.public())
        if job.status == "running":
            _stop_running(job)
            return jsonify(ok=True, job=job.public())
    return jsonify(error=f"Job already {job.status}."), 409


def _stop_running(job: "Job") -> None:
    """Stop a running job's process and everything it started (a render's ffmpeg outlives
    its parent otherwise)."""
    job.cancel_requested = True
    if job.proc and job.proc.poll() is None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(job.proc.pid)], capture_output=True, **NO_CONSOLE)


def _video_url(chapter_id: str, mp4: Path, slug: str | None, ch_no: str | None) -> str | None:
    """URL of a rendered video, versioned by its mtime: stable while the file is
    unchanged (so the player isn't reloaded) and different after a re-render."""
    if not mp4.exists():
        return None
    scope = f"&project={slug}&ch_no={ch_no}" if slug and ch_no else ""
    return f"/output/{chapter_id}/{mp4.name}?v={int(mp4.stat().st_mtime)}{scope}"


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


def _read_editor_state(path: Path) -> dict | None:
    """The saved state at `path`, or None if absent/unreadable. Falls back to the
    .bak of the same file if the primary is corrupt - see atomic_io.py."""
    if not path.exists() and not path.with_name(path.name + ".bak").exists():
        return None
    try:
        state = read_json_with_backup_fallback(path)
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _state_from_mapping(chapter_id: str) -> dict | None:
    """Rebuild editor lines from the exported mapping.json - the on-disk record
    of finished boxing work, used when neither editor save file has any."""
    mapping = _read_editor_state(_framer_dir(chapter_id) / "mapping.json")
    if not mapping or not mapping.get("lines"):
        return None
    return {
        "chapter_id": chapter_id,
        "lines": [{
            "text": ln.get("text", ""),
            "duration_s": ln.get("duration_s", 4),
            "mode": ln.get("mode", "sequence"),
            "pace": ln.get("pace"),
            "frames": [{"bbox": f["bbox"], "flip": f.get("flip", False),
                        "blur": f.get("blur_regions", [])} for f in ln.get("frames", [])],
        } for ln in mapping["lines"]],
    }


def _resolve_editor_state(chapter_id: str) -> dict | None:
    """The chapter's editor state, derived from whatever is on disk: the most
    recently written of editor_state.json / recovery.json / the Claude narration
    pre-fill that actually holds lines, else the exported mapping.json, else any
    (empty) save. Never trusts
    one file just because it exists - an empty or stale one must not shadow
    real work in another."""
    saved = [s for s in (_read_editor_state(_editor_state_path(chapter_id, r)) for r in (False, True)) if s]
    prefilled = _state_from_narration(chapter_id)
    populated = sorted((s for s in [*saved, prefilled] if s and s.get("lines")),
                       key=lambda s: s.get("saved_at", ""), reverse=True)
    if populated:
        return populated[0]
    return _state_from_mapping(chapter_id) or (saved[0] if saved else None)


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
    # Atomic + backed up (see atomic_io.py) - this is the live boxing/script
    # session itself; a half-written editor_state.json/recovery.json from a
    # crash mid-save is exactly the kind of loss this guards against.
    atomic_write_json(_ensure_dir(path.parent) / path.name, state)
    return jsonify(ok=True, path=str(path), saved_at=state["saved_at"])


@app.get("/editor_state/load")
def load_editor_state():
    """Restore a saved session: returns {state, preview}.

    Query: chapter, recovery?(0/1), narration?(0/1: the Claude pre-fill, regardless of newer saves). `preview` is the same payload /load_pdf returns
    (rebuilt from the persisted tiles, NO re-stitch) so the strip is shown again;
    it is null if the chapter has no stitched strip on disk.
    """
    chapter_id = _sanitize_chapter(request.args.get("chapter", ""))
    if request.args.get("recovery") in ("1", "true", "yes"):
        state = _read_editor_state(_editor_state_path(chapter_id, True))
    elif request.args.get("narration") in ("1", "true", "yes"):
        state = _state_from_narration(chapter_id)
    else:
        state = _resolve_editor_state(chapter_id)
    if state is None:
        return jsonify(error=f"No saved editor state for {chapter_id}."), 404
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


@app.get("/chapter_source_status")
def chapter_source_status():
    """The Chapter Source / Stitch Strip panels' TRUE state, derived straight
    from what's actually on disk for `chapter` - NEVER from editor_state.json,
    which only ever held the framer's own boxing data (lines/frames) and
    silently dropped download/split/stitch progress on every reopen. Returns
    {pdf, gemini_parts, strip, video_url}, each null if that step hasn't happened yet.
    `strip` is the same preview payload /editor_state/load returns, so the UI
    can show an already-stitched strip without re-stitching it."""
    chapter_id = _sanitize_chapter(request.args.get("chapter", ""))
    if not chapter_id:
        return jsonify(error="chapter required"), 400

    pdf_path = _downloaded_pdf(chapter_id)
    pdf = ({"path": str(pdf_path), "name": pdf_path.name,
            "page_count": _pdf_page_count(pdf_path)}
           if pdf_path is not None else None)

    parts = _existing_gemini_parts(chapter_id)
    gemini_parts = {"dir": str(_gemini_parts_dir(chapter_id)), "parts": parts} if parts else None

    slug, ch_no = _scope_params()
    video_url = _video_url(chapter_id, chapter_output_dir(chapter_id) / "recap.mp4", slug, ch_no)
    return jsonify(pdf=pdf, gemini_parts=gemini_parts, strip=_preview_payload(chapter_id),
                   video_url=video_url)


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
# Dependency self-repair - pulling new CODE (below) can't install a new
# PACKAGE that code needs, and .venv is deliberately untouched by git. deps.py
# compares every requirements file against what the venv really has and runs
# the same CUDA-torch-safe install that setup.bat uses. It runs as a subprocess,
# never imported, so a pull that changes deps.py itself takes effect at once.
# System tools pip can't install (ffmpeg, git, Claude Code) are reported by
# preflight.system_tools().
# --------------------------------------------------------------------------- #
FEATURE_PACKAGES = {
    "flask": "the app itself",
    "PyMuPDF": "PDF page rendering",
    "Pillow": "image processing (compositing, previews, thumbnails)",
    "requests": "chapter metadata lookup",
    "beautifulsoup4": "chapter metadata lookup",
    "pystray": "the system tray icon",
    "python-dotenv": "reading saved API keys from .env",
    "google-genai": "AI script generation (Settings -> Script generation)",
    "keyring": "storing the Gemini API key in Windows Credential Manager instead of a plaintext file",
    "opencv-python-headless": "speech-bubble detection (Claude script engine)",
    "torch": "AI narration + GPU acceleration",
    "torchaudio": "AI narration + GPU acceleration",
    "chatterbox-tts": "AI narration (Chatterbox voice)",
    "kokoro": "AI narration (Kokoro voice)",
    "soundfile": "voice reference processing",
    "numpy": "audio processing",
    "edge-tts": "the draft (fast, no-GPU) narration mode",
    "mutagen": "the draft narration mode's clip-length reading",
    "moviepy": "video rendering (fallback path)",
}


def _deps_cli(*args: str) -> list[str]:
    return [sys.executable, str(ROOT / "deps.py"), *args]


def _missing_packages() -> list[dict]:
    out = subprocess.run(_deps_cli("check", "--json"), cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8", timeout=120, **NO_CONSOLE).stdout
    return [{"package": n, "feature": FEATURE_PACKAGES.get(n, "part of the app")}
            for n in json.loads(out.strip().splitlines()[-1])]


def _system_tool_problems() -> list[dict]:
    return [tool for tool in preflight.system_tools() if tool["status"] != "ok"]


@app.get("/api/gpu/status")
def api_gpu_status():
    return jsonify(gpu.status())


@app.get("/api/deps/check")
def api_deps_check():
    return jsonify(missing=_missing_packages(), tools=_system_tool_problems())


def _install_deps_stream():
    proc = subprocess.Popen(_deps_cli("install"), cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, bufsize=1,
                            encoding="utf-8", errors="replace", **NO_CONSOLE)
    for line in proc.stdout:
        yield {"type": "log", "line": line.rstrip("\n")}
    if proc.wait() != 0:
        yield {"type": "error", "message": "Installing the packages failed - see the log above."}
        return
    cuda_ok = (gpu.torch_info() or {}).get("available") if gpu.nvidia_gpu() else None
    yield {"type": "result", "missing": _missing_packages(), "cuda_ok": cuda_ok}


@app.get("/api/deps/install_stream")
def api_deps_install_stream():
    def stream():
        try:
            for item in _install_deps_stream():
                if item["type"] == "result":
                    yield _sse({"type": "done", "ok": not item["missing"],
                                "missing": item["missing"], "cuda_ok": item["cuda_ok"]})
                else:
                    yield _sse(item)
        except GeneratorExit:
            raise
        except Exception as e:  # noqa: BLE001 - last-resort: still report, don't crash the stream
            yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


# --------------------------------------------------------------------------- #
# in-app update - "Check for updates" pulls the app's own CODE from git.
# Deliberately just `git pull --ff-only` in ROOT: nothing here touches .venv,
# installed packages, or the Hugging Face model cache - those all live
# outside what git tracks (see .gitignore), so a pull can never reach them.
# GIT_TERMINAL_PROMPT=0 makes a missing/expired credential fail fast with a
# clear error instead of hanging the request on an invisible auth prompt.
# Every success path also reports currently-missing packages (see
# _missing_packages above) - a pull can leave the CODE ahead of the venv's
# installed packages, and this is the only place that would ever notice.
#
# Tracked code files are never meant to be user-edited, and nothing in this
# app writes to them - so a local modification to one is always checkout
# noise (most likely a previous pull that got interrupted mid-checkout, e.g.
# the PC slept/lost power/was force-closed while git was writing files - a
# `git pull --ff-only` can't tell that apart from a real edit and refuses
# with "local changes would be overwritten by merge", which then blocks
# EVERY future update on that machine forever until someone runs `git reset
# --hard` by hand. `git reset --hard HEAD` before every pull (below) makes
# this self-healing: it only touches TRACKED files, matching them back to
# HEAD - it can't reach projects/, work/, output/, .env, or anything else
# under this app's folder, all of which are untracked/gitignored.
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
                               capture_output=True, text=True, timeout=120, **NO_CONSOLE)

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
                            capture_output=True, text=True, timeout=30, **NO_CONSOLE)
            old = run_git("rev-parse", "HEAD")
        if old.returncode != 0:
            return jsonify(ok=False, message="Could not read the current version: "
                                              + (old.stderr or old.stdout).strip())
        old_hash = old.stdout.strip()

        reset = run_git("reset", "--hard", "HEAD")
        if reset.returncode != 0:
            return jsonify(ok=False, message="Could not prepare the update: "
                                              + (reset.stderr or reset.stdout).strip())

        pull = run_git("pull", "--ff-only")
        if pull.returncode != 0:
            return jsonify(ok=False, message="Update failed: "
                                              + (pull.stderr or pull.stdout).strip())

        new = run_git("rev-parse", "HEAD")
        new_hash = new.stdout.strip() if new.returncode == 0 else old_hash

        if new_hash == old_hash:
            return jsonify(ok=True, already_up_to_date=True, files_changed=0,
                            message="Already up to date.",
                            missing_packages=_missing_packages(), system_tools=_system_tool_problems())

        diff = run_git("diff", "--name-only", old_hash, new_hash)
        changed = [ln for ln in (diff.stdout or "").splitlines() if ln.strip()]
        missing = _missing_packages()

        if missing:
            message = (f"Updated {len(changed)} file(s). {len(missing)} required "
                       f"package(s) aren't installed yet - see below.")
        else:
            message = f"Updated {len(changed)} file(s). Restart the app to apply."

        return jsonify(ok=True, already_up_to_date=False, files_changed=len(changed),
                        message=message, missing_packages=missing, system_tools=_system_tool_problems())
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, message="Update timed out - check your internet "
                                          "connection and try again.")
    except FileNotFoundError:
        return jsonify(ok=False, message="git is not installed or not on PATH.")
    except Exception as e:  # noqa: BLE001 - report, don't crash the app
        return jsonify(ok=False, message=f"{type(e).__name__}: {e}")


@app.post("/api/restart")
def api_restart():
    """Cleanly restart this server process - what the "Restart now" button
    (shown after a successful "Check for updates") uses instead of asking
    the user to close and reopen start.bat themselves.

    NOT os.execv(): tried it first, but on Windows it's emulated (there's no
    real exec()) via spawn-then-exit, and called from a background thread
    while the main thread sits in Werkzeug's blocking accept() loop, it left
    the OLD process alive-but-unresponsive (still holding the port, no
    longer answering requests) instead of actually being replaced -
    confirmed empirically, not theoretical. Spawning a genuinely separate
    process and then hard-exiting this one is the reliable version of the
    same idea: no special creationflags, so the new process inherits this
    console (same window, Ctrl+C/close still works) and keeps running once
    this one exits. The new process's own startup (_resolve_port) waits out
    the brief gap until this one actually releases the port. The frontend
    polls /__app_ping__ until the new process answers, then reloads.
    """
    def _do_restart():
        time.sleep(0.3)  # let this response flush before we spawn + exit
        env = dict(os.environ, RECAPPER_SKIP_BROWSER_OPEN="1")
        subprocess.Popen([sys.executable] + sys.argv, cwd=str(ROOT), env=env)
        os._exit(0)  # hard exit: no WSGI response to finish, no cleanup needed
    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
# project system - list/create/open projects, chapter status, media settings +
# live previews. See tools/framer/projects.py (storage) and tools/framer/sites/
# (the best-effort per-site series scrapers). Chapter-scoped routes above are
# entered via the before_request hook near the top of this file whenever a
# request carries `project` + `ch_no`.
# --------------------------------------------------------------------------- #
@app.get("/api/projects")
def api_list_projects():
    return jsonify(projects=projects.list_projects())


@app.post("/api/projects")
def api_create_project():
    """Body: JSON {name, url}. Creates the project, then tries to auto-fetch
    series details (title, chapter count, chapter list) from whichever site
    adapter (see tools/framer/sites/) matches `url`. NEVER blocks on a failed
    fetch - `fetch.ok` tells the UI whether to fall back to the manual "enter
    chapter range 1..N" input."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    url = (data.get("url") or "").strip()
    if not name:
        return jsonify(error="Project name required."), 400
    project = project_defaults.create_project(name, url)

    adapter = sites.detect(url) if url else None
    fetched = adapter.fetch_series(url) if adapter else None
    if adapter:
        project["site"] = adapter.SITE_NAME
    if fetched:
        project["series_title"] = fetched["title"]
        project["source"] = "scraped"
        projects.fill_chapters(project, fetched["chapters"])
        fetch_result = {"ok": True, "title": fetched["title"], "total": fetched["total"]}
    else:
        fetch_result = {"ok": False, "reason": "Couldn't read the series page - enter "
                                                "the chapter range manually."}
    projects.save(project)

    return jsonify(slug=project["slug"], project=project, fetch=fetch_result)


def _stream_task(task: Callable[[Callable[[dict], None], threading.Event], dict]) -> Response:
    """Run `task(emit, stop)` on a thread and stream what it emits as SSE frames, ending with a `done` frame
    carrying its result or an `error` frame. `stop` is set when the client goes away."""
    events: queue.Queue = queue.Queue()
    stop = threading.Event()

    def work() -> None:
        try:
            events.put({"type": "done", **task(events.put, stop)})
        except Exception as e:  # noqa: BLE001 - the UI shows the message
            events.put({"type": "error", "message": str(e)})
        events.put(None)

    def stream():
        threading.Thread(target=work, daemon=True).start()
        try:
            while (event := events.get()) is not None:
                yield _sse(event)
        finally:
            stop.set()

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.get("/api/projects/<slug>/missing_chapters")
def api_missing_chapters(slug):
    """How many of the project's chapters are not yet stored in its project folder."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    return jsonify(count=len(bulk_download.missing_chapters(slug)), total=len(projects.load(slug)["chapters"]))


@app.get("/api/projects/<slug>/download_stream")
def api_bulk_download(slug):
    """Download the next `count` chapters not yet stored in the project folder. Frames: log, progress
    {chapter, position, total}; done {requested, failed:[{chapter, error}]}."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    project = projects.load(slug)
    if not project["site"]:
        return jsonify(error="This project has no remembered source to download from."), 400
    try:
        count = int(request.args.get("count", ""))
    except ValueError:
        return jsonify(error="count must be a number of chapters."), 400

    def task(emit, stop):
        def log(line):
            emit({"type": "log", "line": line})

        def download_one(n: int) -> None:
            with config.chapter_scope(projects.chapter_work_dir(slug, n), projects.chapter_output_dir(slug, n)):
                _download_chapter(project["url"], str(n), f"{slug}_ch{n}", slug, str(n), log)

        def progress(chapter: int, position: int, total: int) -> None:
            emit({"type": "progress", "chapter": chapter, "position": position, "total": total})

        return bulk_download.run(slug, max(count, 0), download_one, progress, stop.is_set)

    return _stream_task(task)


@app.get("/api/app_settings")
def api_get_app_settings():
    folder = app_settings.projects_folder()
    return jsonify(projects_folder=str(folder) if folder else "")


@app.post("/api/app_settings")
def api_set_app_settings():
    folder = (request.get_json(silent=True) or {}).get("projects_folder")
    try:
        saved = app_settings.set_projects_folder(folder or "")
    except OSError as e:
        return jsonify(error=f"Couldn't use that folder: {e}"), 400
    return jsonify(projects_folder=str(saved) if saved else "")


@app.get("/api/projects/<slug>")
def api_get_project(slug):
    """The project with every chapter's stored fields and status. The per-chapter detail (tone,
    narration estimate, video length, steps, thumbnail) is costly to gather from disk, so it is added
    only to the first `limit` chapters - the page of the chapter list being shown."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    project = projects.derive_all(slug)
    limit = request.args.get("limit", type=int)
    project["detail_limit"] = limit if limit is not None else len(project["chapters"])
    chars_per_sec = totals.speech_rate(slug) or claude_engine.DEFAULT_CHARS_PER_SEC
    for ch in project["chapters"][:project["detail_limit"]]:
        ch["effective_tone"] = projects.chapter_tone(project, ch["n"])
        ch["narration"] = _narration_estimate(slug, ch["n"], chars_per_sec)
        video = projects.chapter_video_path(slug, ch["n"])
        ch["duration"] = branding.duration_of(video) if video.is_file() else None
        ch["steps"] = _chapter_steps(slug, ch["n"])
        ch["has_thumb"] = _thumb_source(slug, ch["n"]) is not None
    project["claude_usage"] = totals.claude_usage(slug)
    return jsonify(project=project)


@app.get("/api/projects/<slug>/chapters/<int:n>/thumb")
def api_chapter_thumb(slug, n):
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    thumb = _chapter_thumb(slug, n)
    return send_file(thumb, mimetype="image/jpeg", max_age=0) if thumb else (jsonify(error="No frame yet."), 404)


@app.get("/api/projects/<slug>/export")
def api_export_project(slug):
    """Zip the whole project folder (downloads, frames, audio, renders, assets,
    settings) for backup or transfer. `?videos=0` leaves out rendered videos."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        projects.export_archive(slug, Path(tmp), include_videos=request.args.get("videos") != "0")
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    resp = send_file(tmp, as_attachment=True, download_name=f"{slug}.recapper.zip",
                     mimetype="application/zip")
    resp.call_on_close(lambda: Path(tmp).unlink(missing_ok=True))
    return resp


@app.post("/api/projects/import")
def api_import_project():
    """Multipart: {file} - a zip made by the export route. Always creates a new
    project (renamed if the slug is taken), never overwrites one."""
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify(error="file required"), 400
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        upload.save(tmp)
        project = projects.import_archive(Path(tmp))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    finally:
        Path(tmp).unlink(missing_ok=True)
    return jsonify(slug=project["slug"], project=project)


@app.delete("/api/projects/<slug>")
def api_delete_project(slug):
    """Body: JSON {confirm} - must be the project's exact name. Removes the project's files from every
    location (see projects.delete)."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    if (request.get_json(silent=True) or {}).get("confirm") != projects.load(slug)["name"]:
        return jsonify(error="Type the project's exact name to confirm."), 400
    running = _BATCHES.get(slug)
    if (running and running.active()) or any(j.slug == slug and j.status in ("queued", "running") for j in _JOBS.values()):
        return jsonify(error="This project is busy - cancel its pipeline and jobs first."), 409
    try:
        projects.delete(slug)
    except OSError as e:
        return jsonify(error=str(e)), 500
    _BATCHES.pop(slug, None)
    return jsonify(ok=True)


@app.post("/api/projects/<slug>/chapters")
def api_set_chapters(slug):
    """Body: JSON {from, to} for the manual fallback range, or {refetch:true} to
    retry the source-site scrape."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    data = request.get_json(silent=True) or {}
    project = projects.load(slug)

    if data.get("refetch"):
        adapter = sites.detect(project["url"])
        fetched = adapter.fetch_series(project["url"]) if adapter else None
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


@app.post("/api/projects/<slug>/chapters/<int:n>/tone")
def api_set_chapter_tone(slug, n):
    """Body: JSON {tone}: one of tones.TONES, or null to follow the project's default tone."""
    try:
        ch = projects.set_chapter_tone(slug, n, (request.get_json(silent=True) or {}).get("tone"))
    except (ValueError, KeyError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True, chapter=ch)


@app.get("/api/tts/kokoro/voices")
def api_kokoro_voices():
    return jsonify(voices=tts_kokoro.VOICES)


@app.get("/api/tts/kokoro/preview")
def api_kokoro_preview():
    """A short sample of the Kokoro voice in `?voice=`, as a wav."""
    voice = request.args.get("voice", "")
    if voice not in tts_kokoro.VOICES:
        return jsonify(error="Unknown Kokoro voice."), 400
    try:
        return Response(tts_kokoro.sample_wav(voice), mimetype="audio/wav")
    except ImportError:
        return jsonify(error="Kokoro is not installed. Run setup.bat again, or: pip install kokoro misaki[en]"), 409


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


@app.post("/api/projects/<slug>/defaults")
def api_save_defaults(slug):
    """Make this project's settings (and the custom files they use) the defaults for new projects."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    project_defaults.save_from(slug)
    return jsonify(ok=True)


@app.get("/api/defaults/changes")
def api_defaults_changes():
    """What applying the default settings would change, for `?project=<slug>` or every project."""
    slugs = [request.args["project"]] if request.args.get("project") else [p["slug"] for p in projects.list_projects()]
    found = [{"slug": slug, "name": projects.load(slug)["name"], "changes": project_defaults.changes(slug)}
             for slug in slugs if projects.exists(slug)]
    return jsonify(projects=[p for p in found if p["changes"]])


@app.post("/api/defaults/apply")
def api_apply_defaults():
    """Body: JSON {projects: [slug, ...]}. Applies the default settings to each."""
    slugs = (request.get_json(silent=True) or {}).get("projects") or []
    if not slugs or not all(projects.exists(slug) for slug in slugs):
        return jsonify(error="Choose projects to apply the defaults to."), 400
    for slug in slugs:
        project_defaults.apply(slug)
    return jsonify(ok=True)


@app.get("/api/background_options")
def api_background_options():
    """The ready-made background videos; `?project=<slug>` also says which one that project uses."""
    slug = request.args.get("project")
    return jsonify(options=project_defaults.background_options(),
                   current=project_defaults.current_background_option(slug) if slug and projects.exists(slug) else None)


@app.get("/api/background_options/<option_id>")
def api_background_option_video(option_id):
    path = project_defaults.background_option(option_id)
    return send_file(path, conditional=True) if path else (jsonify(error="No such background video."), 404)


@app.post("/api/projects/<slug>/background")
def api_select_background(slug):
    """Body: JSON {option}. Use that ready-made video as the project's background."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    try:
        project_defaults.select_background(slug, (request.get_json(silent=True) or {}).get("option") or "")
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.post("/api/projects/<slug>/asset")
def api_upload_asset(slug):
    """Multipart: {kind: music|voice|watermark|background|intro|outro, file}. Saves the
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
    else:
        project["settings"]["branding"][kind]["file"] = rel_path
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
    else:
        project["settings"]["branding"][kind]["file"] = None
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
    chapters_dir = projects.local_dir(slug) / "chapters"
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
    still = s7._compose_still([str(sample)], wm=wm)
    return _png_response(still.resize((960, 540), Image.LANCZOS))


@app.get("/api/projects/<slug>/preview/background.png")
def api_preview_background(slug):
    """Query: dim. Uses the already-saved background asset (see /asset)."""
    bg_asset = projects.find_asset(slug, "background")
    dim = request.args.get("dim", 0.85)
    bg = s7._load_background({"background": {"file": str(bg_asset) if bg_asset else None,
                                              "dim": dim}})
    sample = _placeholder_frame(slug)
    still = s7._compose_still([str(sample)], bg)
    return _png_response(still.resize((960, 540), Image.LANCZOS))


# --------------------------------------------------------------------------- #
# merge multiple chapters' rendered videos into one, in chapter order. Purely
# a framer-side feature over already-rendered recap.mp4 files - it reads
# finished output, nothing about the manifest/orchestrator/pipeline stages
# needs to change for this.
# --------------------------------------------------------------------------- #
_MERGE_JOBS: dict[str, "subprocess.Popen | None"] = {}
_MERGE_LOCK = threading.Lock()


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


def _build_merge_cmd_reencode(ffmpeg: str, size: tuple[int, int], paths: list[Path], out: Path) -> list[str]:
    """Safe path for mismatched inputs: scale/letterbox every video to
    the first video's size @24fps (never cropped or stretched) and resample audio to a
    common format before concatenating, so a stray non-standard render can't
    corrupt or desync the merge."""
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats"]
    for p in paths:
        cmd += ["-i", str(p)]
    parts, vlabels, alabels = [], [], []
    for i in range(len(paths)):
        parts.append(f"[{i}:v]scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease,"
                     f"pad={size[0]}:{size[1]}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p[v{i}]")
        parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=mono[a{i}]")
        vlabels.append(f"[v{i}]")
        alabels.append(f"[a{i}]")
    concat_inputs = "".join(v + a for v, a in zip(vlabels, alabels))
    parts.append(f"{concat_inputs}concat=n={len(paths)}:v=1:a=1[vout][aout]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "30", "-c:a", "aac", "-b:a", "64k",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    return cmd


@app.get("/api/projects/<slug>/merge_stream")
def merge_stream(slug):
    """Concatenate several chapters' recap.mp4, in chapter order, into one
    video under projects/<slug>/merged/. EventSource (GET) only:
        ?chapters=1,3,5
    Only chapters whose recap.mp4 exists on disk are accepted. When every input shares 1920x1080 +
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
            yield _sse({"type": "error", "message": "Select at least 2 chapters with a video to merge."})
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
                video = projects.chapter_video_path(slug, n)
                if not video.is_file():
                    yield _sse({"type": "error", "message": f"Chapter {n} has no rendered recap.mp4."})
                    return
                paths.append(video)

            yield _sse({"type": "log", "line": f"Probing {len(paths)} video(s)…"})
            infos = []
            for n, p in zip(chapters, paths):
                info = branding.probe(p)
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
                and len({(i.get("audio") or {}).get("codec_name") for i in infos}) == 1
                and len({(i.get("audio") or {}).get("sample_rate") for i in infos}) == 1
            )
            out_dir = projects.merged_dir(slug)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            name = f"merged_{stamp}_ch" + "-".join(str(n) for n in chapters) + ".mp4"
            out = out_dir / name
            ffmpeg = gpu.ffmpeg_path()

            if uniform:
                yield _sse({"type": "log", "line": "All inputs share one size and codec - "
                                                    "fast stream-copy merge (no re-encode)."})
                list_file = out_dir / "_concat_list.txt"
                _write_concat_list(paths, list_file)
                cmd = _build_merge_cmd_copy(ffmpeg, list_file, out)
            else:
                size = (infos[0]["video"]["width"], infos[0]["video"]["height"])
                yield _sse({"type": "log",
                            "line": f"Inputs differ in resolution/codec - normalizing every clip to "
                                    f"{size[0]}x{size[1]}@24fps before merging (slower)."})
                cmd = _build_merge_cmd_reencode(ffmpeg, size, paths, out)

            yield _sse({"type": "log", "line": "$ " + " ".join(cmd)})
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    text=True, bufsize=1, encoding="utf-8", errors="replace",
                                    **NO_CONSOLE)
            with _MERGE_LOCK:
                _MERGE_JOBS[slug] = proc
            for line in proc.stdout:
                yield _sse({"type": "log", "line": line.rstrip("\n")})
            proc.wait()

            if proc.returncode == 0 and out.is_file():
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

# --------------------------------------------------------------------------- #
# merged videos: the on-disk list, intro/outro "final" renders (queued like any
# other job, see _run_brand_job) and deletion. Branding never touches chapter
# renders - see branding.py.
# --------------------------------------------------------------------------- #
def _brand_key(slug: str, name: str) -> str:
    return f"brand:{slug}:{name}"


def _busy(key: str) -> bool:
    with _JOBS_LOCK:
        return key in _ACTIVE_CHAPTERS


@app.get("/api/projects/<slug>/merges")
def api_list_merges(slug):
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    return jsonify(merges=branding.list_merges(slug), folder=str(projects.merged_dir(slug)),
                   branding=projects.load(slug)["settings"]["branding"],
                   default_clips={k: p.name for k, p in branding.DEFAULT_CLIPS.items()})


@app.get("/api/projects/<slug>/branding/<kind>")
def api_branding_clip(slug, kind):
    """The clip that will actually be used for `kind` (custom upload or bundled default)."""
    if kind not in branding.CLIP_KINDS or not projects.exists(slug):
        return jsonify(error="unknown project or clip"), 404
    try:
        clip = branding.clip_source(slug, kind)
    except FileNotFoundError as e:
        return jsonify(error=str(e)), 404
    return send_from_directory(str(clip.parent), clip.name)


@app.post("/api/projects/<slug>/merges/<name>/final")
def api_render_final(slug, name):
    """Queue the branded version of a merged video (intro/outro per the project's
    branding settings). Writes `<merge>_branded.mp4`; the raw merge is untouched."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    try:
        raw = branding.merge_path(slug, name)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if not raw.is_file():
        return jsonify(error=f"{name} not found."), 404
    settings = projects.load(slug)["settings"]["branding"]
    if not any(settings[k]["enabled"] for k in branding.CLIP_KINDS):
        return jsonify(error="Turn on the intro or the outro first."), 400
    key = _brand_key(slug, name)
    if _busy(key):
        return jsonify(error="A final render is already queued or running for this video."), 409
    job = Job(key, "brand", slug, None, 0, target=name)
    job.append_log(f"Queued final render of {name}…")
    _enqueue(job)
    return jsonify(ok=True, job_id=job.id, job=job.public())


@app.delete("/api/projects/<slug>/merges/<name>")
def api_delete_merge(slug, name):
    """Delete a merged video and its branded file, or only the branded file (?final=1)."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    if _busy(_brand_key(slug, name)):
        return jsonify(error="A final render is in progress for this video."), 409
    try:
        branding.delete_merge(slug, name, final_only=request.args.get("final") == "1")
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
# media library: sizes per project / chapter / file type, and deleting whole
# regenerable types. Only types known to library.KINDS are reachable here; the
# work itself (script, mapping, editor state, settings, uploads) is not.
# --------------------------------------------------------------------------- #
@app.get("/api/projects/<slug>/library")
def api_library(slug):
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    merges = branding.list_merges(slug)
    merged_bytes = sum(m["size"] + (m["final"] or {}).get("size", 0) for m in merges)
    return jsonify(**library.overview(slug, merged_bytes), merges=merges)


@app.get("/api/projects/<slug>/library/<int:n>")
def api_library_chapter(slug, n):
    if not projects.exists(slug) or projects.get_chapter(projects.load(slug), n) is None:
        return jsonify(error="No such chapter."), 404
    return jsonify(library.chapter_detail(slug, n))


@app.get("/api/projects/<slug>/library/<int:n>/file")
def api_library_file(slug, n):
    path = library.servable_file(slug, n, request.args.get("path", ""))
    if path is None:
        return jsonify(error="No such file."), 404
    return send_from_directory(str(path.parent), path.name)


@app.post("/api/projects/<slug>/library/delete")
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


# --------------------------------------------------------------------------- #
# Batch automation (batch.py): the whole pipeline for several chapters of a project. Each
# stage runner calls the same functions the matching button's route does.
# --------------------------------------------------------------------------- #
_BATCHES: dict[str, batch.Batch] = {}
_BATCH_LOCK = threading.Lock()
THUMB_WIDTH = 480


def _entries_from_state(state: dict) -> list[dict]:
    """Export entries for editor-state lines, as the editor's Export button builds them."""
    return [{"line_text": line["text"], "duration_s": line.get("duration_s") or 4, "mode": line.get("mode"),
             "pace": line.get("pace"),
             "frames": [{"bbox": f["bbox"], "flip": bool(f.get("flip")), "blur": f.get("blur") or []}
                        for f in line.get("frames", [])]} for line in state["lines"]]


def _chapter_pdf(chapter_id: str) -> Path:
    pdf = _downloaded_pdf(chapter_id)
    if pdf is None:
        raise Refused("Download the chapter first.")
    return pdf


def _newest(paths) -> float | None:
    times = [p.stat().st_mtime for p in paths if p.is_file()]
    return max(times) if times else None


def _script_saved_at(state_path: Path) -> float | None:
    narration = claude_engine.read_narration(state_path)
    return datetime.fromisoformat(narration["saved_at"]).timestamp() if narration else None


def _stage_times(chapter_id: str) -> dict[str, float | None]:
    """When each pipeline step's output was last written, None for a step with no output. Creates
    nothing on disk, so it is safe to call for every chapter. Call inside its scope."""
    work = chapter_work_dir(chapter_id)
    try:
        beats = Manifest.load(work).beats
    except (OSError, ValueError):
        beats = []
    voiced = bool(beats) and all(beat.audio and Path(beat.audio).is_file() for beat in beats)
    return {
        "download": _newest((work / "download").rglob("*.pdf")),
        "split": _newest((work / "gemini_parts").glob("*.pdf")),
        "stitch": _newest([work / "pages" / "strip_001.png"]),
        "script": _script_saved_at(work / "claude" / "state.json"),
        "export": _newest([work / "framer" / "mapping.json"]),
        "audio": _newest(Path(beat.audio) for beat in beats) if voiced else None,
        "video": _newest([chapter_output_dir(chapter_id) / "recap.mp4"]),
    }


def _chapter_steps(slug: str, n: int) -> dict:
    """The chapter's pipeline steps as the disk has them: `done` (a rendered video counts as having
    every step, older chapters never produced some intermediates), `stale` (done, but an earlier
    step was written after it), and `next`, the first step that is missing or stale."""
    with config.chapter_scope(projects.chapter_work_path(slug, n), projects.chapter_output_path(slug, n)):
        times = _stage_times(f"{slug}_ch{n}")
    rendered = times["video"] is not None
    done = [s for s in batch.LANE_STAGES if rendered or times[s] is not None]
    stale = [s for i, s in enumerate(batch.LANE_STAGES)
             if times[s] is not None and any(t is not None and t > times[s] for t in map(times.get, batch.LANE_STAGES[:i]))]
    pending = [s for s in batch.LANE_STAGES if s not in done or s in stale]
    return {"done": done, "stale": stale, "next": pending[0] if pending else None}


def _stages_on_disk(chapter_id: str) -> list[str]:
    """The leading run of steps the chapter has output for: what a batch resumes from.
    Call inside its scope."""
    times = _stage_times(chapter_id)
    rendered = times["video"] is not None
    done = []
    for stage in batch.LANE_STAGES:
        if not rendered and times[stage] is None:
            break
        done.append(stage)
    return done


def _thumb_source(slug: str, n: int) -> tuple[Path, bool] | None:
    """The file the chapter's thumbnail comes from (its first exported frame, else its rendered
    video) and whether that file is a frame."""
    frames = sorted((projects.chapter_work_path(slug, n) / "frames").glob("frame_*.png"))
    source = frames[0] if frames else projects.chapter_video_path(slug, n)
    return (source, bool(frames)) if source.is_file() else None


def _chapter_thumb(slug: str, n: int) -> Path | None:
    """A small JPEG of the chapter's thumbnail source, rebuilt whenever that source is newer."""
    found = _thumb_source(slug, n)
    if found is None:
        return None
    source, is_frame = found
    thumb = projects.chapter_work_dir(slug, n) / "thumb.jpg"
    if thumb.is_file() and thumb.stat().st_mtime >= source.stat().st_mtime:
        return thumb
    if is_frame:
        with Image.open(source) as image:
            image.convert("RGB").resize((THUMB_WIDTH, round(image.height * THUMB_WIDTH / image.width))).save(thumb, quality=82)
    else:
        done = subprocess.run([gpu.ffmpeg_path(), "-y", "-ss", "1", "-i", str(source), "-frames:v", "1",
                               "-vf", f"scale={THUMB_WIDTH}:-2", str(thumb)], capture_output=True, **NO_CONSOLE)
        if done.returncode:
            return None
    return thumb


def _cancel_job(job_id: str) -> None:
    job = _JOBS.get(job_id)
    if job and job.status == "running":
        _stop_running(job)


def _batch_env(slug: str) -> batch.BatchEnv:
    def chapter_id(n: int) -> str:
        return f"{slug}_ch{n}"

    def batch_settings() -> dict:
        return projects.load(slug)["settings"]["batch"]

    def mirror(b: batch.Batch, n: int, line: str) -> None:
        b.log(n, line)

    def run_job(b: batch.Batch, n: int, job: Job) -> None:
        b.attach(n, job.id)
        job.mirror = lambda line: mirror(b, n, line)
        _run_now(job)
        if job.status != "done":
            raise RuntimeError(job.message or f"job {job.status}")

    def download(b: batch.Batch, n: int) -> None:
        _download_chapter(projects.load(slug)["url"], str(n), chapter_id(n), slug, str(n), lambda line: b.log(n, line))

    def split(b: batch.Batch, n: int) -> None:
        for event in _build_gemini_parts_stream(_chapter_pdf(chapter_id(n)), chapter_id(n)):
            if event["type"] == "log":
                b.report(n, event["line"])

    def stitch(b: batch.Batch, n: int) -> None:
        _stitch_chapter(_chapter_pdf(chapter_id(n)), chapter_id(n))

    def script(b: batch.Batch, n: int) -> None:
        b.gate()
        job = _new_script_job(chapter_id(n), slug, str(n))
        job.before_claude_call = b.gate
        run_job(b, n, job)

    def export(b: batch.Batch, n: int) -> None:
        state = _resolve_editor_state(chapter_id(n))
        if not state or not state.get("lines"):
            raise Refused("There are no narration lines to export.")
        _export_chapter(chapter_id(n), _entries_from_state(state), slug, str(n))

    def render(what: str) -> Callable[[batch.Batch, int], None]:
        return lambda b, n: run_job(b, n, _new_render_job(chapter_id(n), what, slug, str(n)))

    def video(b: batch.Batch, n: int) -> None:
        render("video")(b, n)
        projects.set_status(slug, str(n), "complete")

    return batch.BatchEnv(
        runners={"download": download, "split": split, "stitch": stitch, "script": script,
                 "export": export, "audio": render("voice"), "video": video},
        scope=lambda n: config.chapter_scope(projects.chapter_work_dir(slug, n), projects.chapter_output_dir(slug, n)),
        done_stages=lambda n: _stages_on_disk(chapter_id(n)),
        review_enabled=lambda: batch_settings()["review_before_audio"],
        thresholds=lambda: {"five_hour": batch_settings()["five_hour_pct"],
                            "seven_day": batch_settings()["seven_day_pct"]},
        auto_wait=lambda: batch_settings()["auto_wait_five_hour"],
        cancel_job=_cancel_job,
        notify=lambda title, message: notify.toast(title, message) if batch_settings()["notifications"] else None,
        claude_total=lambda n: (claude_engine.read_usage(projects.chapter_work_path(slug, n) / "claude" / "state.json")
                                or {}).get("total"))


def _batch_path(slug: str) -> Path:
    return projects.local_dir(slug) / "batch.json"


def _resume_batches() -> None:
    """Pick up each project's batch where the last run left it."""
    for folder in sorted(PROJECTS_DIR.iterdir()) if PROJECTS_DIR.is_dir() else []:
        if (folder / "batch.json").is_file() and (resumed := batch.Batch.load(_batch_path(folder.name),
                                                                            _batch_env(folder.name))):
            _BATCHES[folder.name] = resumed


def _job_summary(job_id: str) -> dict | None:
    job = _JOBS.get(job_id)
    return {**{k: v for k, v in job.public().items() if k != "log"}, "last_activity": job.last_activity} if job else None


@app.get("/api/projects/<slug>/batch")
def api_batch(slug):
    """The project's batch (or null) with each chapter's live job, plus subscription usage."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    current = _BATCHES.get(slug)
    return jsonify(batch=current.view(_job_summary) if current else None, usage=claude_usage.snapshot(),
                   thresholds=projects.load(slug)["settings"]["batch"])


@app.get("/api/projects/<slug>/batch/preflight")
def api_batch_preflight(slug):
    """Checks to run before a batch of `?chapters=1,2,3` running `?steps=script,export` starts."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    numbers = [int(n) for n in request.args.get("chapters", "").split(",") if n.strip().isdigit()]
    steps = {s for s in request.args.get("steps", "").split(",") if s in batch.LANE_STAGES}
    return jsonify(checks=preflight.check(projects.load(slug), numbers, steps))


@app.post("/api/projects/<slug>/batch")
def api_batch_start(slug):
    """Body: JSON {plans: {"<chapter>": [step, ...]}}. Runs just those steps, in pipeline order, for
    each chapter, pipelined across chapters. A batch that is already running takes them on as well."""
    if not projects.exists(slug):
        return jsonify(error=f"No such project: {slug}"), 404
    known = {c["n"] for c in projects.load(slug)["chapters"]}
    try:
        plans = {int(n): [s for s in batch.LANE_STAGES if s in steps]
                 for n, steps in ((request.get_json(silent=True) or {}).get("plans") or {}).items()}
    except (ValueError, TypeError):
        plans = {}
    if not plans or not set(plans) <= known or not all(plans.values()):
        return jsonify(error="Choose steps to run for chapters of this project."), 400
    if any("script" in steps for steps in plans.values()):
        status = claude_cli.cli_status()
        if not status["ready"]:
            return jsonify(error=status["message"]), 409
    with _BATCH_LOCK:
        previous = _BATCHES.get(slug)
        if previous and previous.active:
            try:
                previous.enqueue(plans)
            except ValueError as e:
                return jsonify(error=str(e)), 409
            return jsonify(ok=True)
        if previous:
            previous.close()
        _BATCHES[slug] = batch.Batch.create(_batch_path(slug), _batch_env(slug), plans)
    return jsonify(ok=True)


def _batch_do(slug: str, action: Callable[[batch.Batch], None]):
    current = _BATCHES.get(slug)
    if current is None:
        return jsonify(error="This project has no batch."), 404
    try:
        action(current)
    except ValueError as e:
        return jsonify(error=str(e)), 409
    return jsonify(ok=True)


@app.post("/api/projects/<slug>/batch/<control>")
def api_batch_control(slug, control):
    """control: pause | resume | cancel, for the whole batch."""
    if control not in ("pause", "resume", "cancel"):
        return jsonify(error="Unknown control."), 404
    return _batch_do(slug, lambda b: getattr(b, control)())


@app.post("/api/projects/<slug>/batch/chapters/<int:n>/continue")
def api_batch_continue(slug, n):
    """Continue chapter n past the frame review."""
    return _batch_do(slug, lambda b: b.continue_review(n))


@app.post("/api/projects/<slug>/batch/usage")
def api_batch_usage(slug):
    """Body: JSON {choice: 'wait' | 'continue'} for the usage limit the batch is holding at."""
    choice = (request.get_json(silent=True) or {}).get("choice")
    return _batch_do(slug, lambda b: b.choose(choice))


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    """True if something is already accepting connections on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _ping_existing(host: str, port: int, timeout: float = 1.0) -> dict | None:
    """If host:port is already serving THIS app (its /__app_ping__ marker),
    return that JSON body (app name + pid) - None if it's unreachable OR
    reachable but NOT ReCapper (some unrelated program happens to be on this
    port), so callers never assume they know what they'd be closing."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/__app_ping__", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data if data.get("app") == "recapper" else None
    except Exception:  # noqa: BLE001 - "can't confirm it's us" -> treat as unknown
        return None


def _find_free_port(host: str, start_port: int, tries: int = 50) -> int:
    for p in range(start_port, start_port + tries):
        if not _port_open(host, p, timeout=0.1):
            return p
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:  # last resort
        s.bind((host, 0))
        return s.getsockname()[1]


def _resolve_port(host: str, port: int) -> int | None:
    """Never let a busy port 5005 fail silently with Werkzeug's raw "Address
    already in use" traceback (which looks like a crash to someone who just
    double-clicked start.bat) - AND never silently start a second, fully
    independent ReCapper server + open a second browser tab just because
    start.bat got launched again while one is already running (the actual
    cause of a real "it keeps opening new tabs on its own" bug report: every
    relaunch is a brand new process, so webbrowser.open() in main() - the
    ONLY place in this app or any of its dependencies that opens a browser,
    confirmed by grepping the whole venv - legitimately fires again each
    time, once per process, exactly as designed; the fix has to be "don't
    start a redundant process" rather than anything about the open() call
    itself). So: if the port is busy and it's confirmed to be OUR OWN
    instance (via /__app_ping__, not just an assumption), the default is to
    do NOTHING - no new server, no new tab, just point at the one already
    running - a relaunch can only ever open a SECOND tab if the person
    explicitly chooses to close the old instance first. Only when the port
    is occupied by some unrelated program does this fall back to the next
    free port (main() still opens exactly one browser tab in that case).
    """
    # A just-restarted instance (see /api/restart) may still be releasing the
    # port - give it a moment before treating it as genuinely occupied.
    for _ in range(20):
        if not _port_open(host, port):
            return port
        time.sleep(0.15)

    print()
    print("=" * 60)
    existing = _ping_existing(host, port)
    if existing:
        pid = existing.get("pid")
        print(f"  ReCapper is already running at http://{host}:{port}/"
              + (f" (PID {pid})" if pid else "") + ".")
        print("  [C] Close it and start fresh here")
        print("  [Enter] Do nothing - switch to that browser tab instead (default)")
        choice = ""
        try:
            choice = input("  Choice [C/Enter]: ").strip().lower()
        except (EOFError, OSError, AttributeError):
            # not an interactive console - either launched by a script, or
            # (since tray_launcher.py runs this under pythonw.exe with no
            # console at all) sys.stdin is None, whose .readline() raises
            # AttributeError rather than EOFError/OSError. Either way: default.
            pass
        if choice == "c" and pid:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                capture_output=True, timeout=10, **NO_CONSOLE)
                print(f"  Closed the previous instance (PID {pid}).")
                for _ in range(20):
                    if not _port_open(host, port):
                        print("=" * 60)
                        return port
                    time.sleep(0.15)
                print(f"  Port {port} is still busy - giving up on closing it.")
            except Exception as e:  # noqa: BLE001
                print(f"  Could not close it ({e}).")
        print(f"  Not starting a second copy. ReCapper is already open at "
              f"http://{host}:{port}/ - this window can be closed.")
        print("=" * 60)
        return None

    print(f"  Port {port} is already in use by another program (not ReCapper).")
    print("  Starting on the next free port instead.")
    new_port = _find_free_port(host, port + 1)
    print(f"  Using http://{host}:{new_port}/ instead.")
    print("=" * 60)
    return new_port


def main() -> None:
    host, port = "127.0.0.1", 5005
    port = _resolve_port(host, port)
    if port is None:
        return   # a duplicate launch of an already-running instance - see _resolve_port()
    url = f"http://{host}:{port}/"
    print("=" * 60)
    print("  ReCapper")
    print(f"  Open: {url}")
    print("=" * 60)
    # Python opens the browser (not start.bat) so it always points at the
    # PORT ACTUALLY USED, which _resolve_port() may have changed. Delayed in
    # a background thread so it doesn't block app.run() below. Skipped after
    # a self-restart (see /api/restart, which sets this env var on the new
    # process) - the tab that triggered the restart already reloads itself
    # once this new instance answers, so opening another one would just be a
    # confusing duplicate. This is the ONLY webbrowser.open() call in the
    # entire app (or any dependency - checked), and it runs at most once per
    # process, so "opens a new tab on its own" can only mean a second
    # process started - which _resolve_port() above now refuses to do.
    if not os.environ.pop("RECAPPER_SKIP_BROWSER_OPEN", None):
        threading.Thread(target=lambda: (time.sleep(1.0), webbrowser.open(url)),
                          daemon=True).start()
    _resume_batches()
    # threaded: a generate job holds an SSE connection open for minutes; other
    # requests (thumbnails, the result video, a second tab) must still be served.
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

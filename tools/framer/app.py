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
from config import chapter_work_dir, chapter_output_dir  # noqa: E402
from manifest import Manifest, Page, Panel  # noqa: E402

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

# Google AI Studio / Gemini accepts at most this many pages per uploaded PDF. A
# downloaded chapter PDF longer than this is split into <=GEMINI_PAGE_LIMIT-page
# chunks at whole-page boundaries so each part can be uploaded for the script.
GEMINI_PAGE_LIMIT = 127
WEBTOON_TIMEOUT_S = 1800  # hard cap on a single webtoon-downloader run (30 min)

HERE = Path(__file__).resolve().parent
app = Flask(__name__)

# Session-wide UI settings, kept SERVER-SIDE in memory (NOT browser localStorage)
# so a theme / layout choice survives page reloads while the app is running.
_SETTINGS = {"theme": "dark", "split": None}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
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


def _split_for_gemini(pdf_path: Path, limit: int) -> list[dict]:
    """Split `pdf_path` into <=`limit`-page parts at whole-page boundaries so each
    can be uploaded to Gemini. A PDF already within the limit is returned as the
    single (unsplit) part. Parts land in `<pdf dir>/split/` and old parts for this
    PDF are cleared first so re-downloading is idempotent. Each entry is
    {path, pages, from, to} with 1-based inclusive page numbers."""
    import fitz

    doc = fitz.open(str(pdf_path))
    try:
        total = doc.page_count
        if total <= limit:
            return [{"path": str(pdf_path), "pages": total, "from": 1, "to": total}]

        out_dir = pdf_path.parent / "split"
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob(f"{pdf_path.stem}_part*.pdf"):
            old.unlink()

        n_parts = math.ceil(total / limit)
        width = len(str(n_parts))
        parts: list[dict] = []
        for idx in range(n_parts):
            a = idx * limit                      # 0-based start
            b = min(a + limit, total)            # exclusive end
            sub = fitz.open()
            sub.insert_pdf(doc, from_page=a, to_page=b - 1)
            out = out_dir / f"{pdf_path.stem}_part{idx + 1:0{width}d}_p{a + 1}-{b}.pdf"
            sub.save(str(out))
            sub.close()
            parts.append({"path": str(out), "pages": b - a, "from": a + 1, "to": b})
        return parts
    finally:
        doc.close()


def _preview_payload(chapter_id: str) -> dict | None:
    """Rebuild the /load_pdf preview payload for an ALREADY-stitched chapter.

    Used by /load_project so a saved session resumes WITHOUT re-stitching the PDF:
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


@app.post("/download")
def download():
    """Download ONE webtoon.com chapter as a PDF, then split it for Gemini.

    Body: JSON {url, chapter_no?, chapter_id?}. Runs webtoon-downloader
    (--save-as pdf) into work/<chapter>/download/, then, if the PDF exceeds
    GEMINI_PAGE_LIMIT pages, splits it into <=limit-page parts at whole-page
    boundaries. Returns {chapter_id, pdf, page_count, split, parts:[{path,pages,
    from,to}], steps:[...]} - `pdf` is the full chapter PDF that feeds Stitch PDF;
    `parts` are the (possibly split) files to hand to Gemini. We do NOT touch
    Gemini - the paths are just reported back."""
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

    parts = _split_for_gemini(pdf, GEMINI_PAGE_LIMIT)
    if len(parts) > 1:
        steps.append(f"{pages} > {GEMINI_PAGE_LIMIT} pages → split into "
                     f"{len(parts)} parts (≤{GEMINI_PAGE_LIMIT} pages each).")
    else:
        steps.append(f"{pages} ≤ {GEMINI_PAGE_LIMIT} pages → no split needed.")

    return jsonify(
        chapter_id=chapter_id,
        pdf=str(pdf),
        page_count=pages,
        split=len(parts) > 1,
        parts=parts,
        steps=steps,
    )


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


@app.post("/load_script")
def load_script():
    """Split pasted text OR a .txt file into ordered narration lines.

    Body: JSON {"text": "..."}  OR  {"txt_path": "..."}. One non-empty line per
    narration segment; surrounding blank lines are dropped.
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

    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
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
    """Run the pipeline's audio+video (or video-only) for a chapter and stream
    progress as SSE. EventSource (GET) only, params in the query string:
        ?chapter=<id>&what=both|video
    `what=both` runs s6 (TTS) then s7 (assemble) via `--from audio`; `what=video`
    re-renders with s7 only via `--from video`. Emits {type:start|log|done|error}
    frames; `done` carries the /output/<ch>/recap.mp4 URL when it exists."""
    chapter_id = _sanitize_chapter(request.args.get("chapter", ""))
    what = (request.args.get("what") or "both").strip().lower()
    from_stage = "video" if what == "video" else "audio"
    manifest = chapter_work_dir(chapter_id) / "manifest.json"

    def stream():
        if not manifest.exists():
            yield _sse({"type": "error",
                        "message": f"No manifest for {chapter_id}. Export from the "
                                   f"Framer first."})
            return

        # reserve the per-chapter slot WITHOUT yielding under the lock
        with _GEN_LOCK:
            busy = chapter_id in _GEN_JOBS
            if not busy:
                _GEN_JOBS[chapter_id] = None
        if busy:
            yield _sse({"type": "error",
                        "message": f"A generate job is already running for "
                                   f"{chapter_id}."})
            return

        total = _beat_count(chapter_id)
        label = "audio + video" if from_stage == "audio" else "video"
        yield _sse({"type": "start", "what": from_stage, "total_beats": total,
                    "message": f"Starting {label} for {chapter_id} "
                               f"({total or '?'} line(s))…"})

        # -u + PYTHONUNBUFFERED so each print streams immediately; utf-8 so the
        # child never dies on a unicode arrow and we decode the pipe cleanly.
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        cmd = [sys.executable, "-u", str(ROOT / "orchestrator.py"),
               "--chapter", chapter_id, "--from", from_stage]
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, bufsize=1,
                encoding="utf-8", errors="replace")
            with _GEN_LOCK:
                _GEN_JOBS[chapter_id] = proc

            yield _sse({"type": "log", "line": "$ " + " ".join(cmd)})
            for line in proc.stdout:                       # streams as it arrives
                yield _sse({"type": "log", "line": line.rstrip("\n")})
            proc.wait()

            if proc.returncode == 0:
                mp4 = chapter_output_dir(chapter_id) / "recap.mp4"
                yield _sse({"type": "done", "ok": True,
                            "video": (f"/output/{chapter_id}/recap.mp4"
                                      if mp4.exists() else None),
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
                _GEN_JOBS.pop(chapter_id, None)

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


# --------------------------------------------------------------------------- #
# save / load project + autosave recovery
# --------------------------------------------------------------------------- #
def _project_path(chapter_id: str, recovery: bool) -> Path:
    return _framer_dir(chapter_id) / ("recovery.json" if recovery else "project.json")


@app.post("/save_project")
def save_project():
    """Persist the FULL working state (lines, frames, modes, durations, strip ref).

    Body: JSON {chapter_id, recovery?:bool, project:{...}}. `recovery` writes the
    periodic autosave (recovery.json) so a closed tab never loses work; a normal
    save writes project.json. The project blob is stored verbatim plus a stamp.
    """
    data = request.get_json(silent=True) or {}
    chapter_id = _sanitize_chapter(data.get("chapter_id") or "")
    project = data.get("project")
    if not chapter_id or not isinstance(project, dict):
        return jsonify(error="chapter_id and project required"), 400
    recovery = bool(data.get("recovery"))
    project = dict(project)
    project["chapter_id"] = chapter_id
    project["saved_at"] = _now_iso()
    path = _project_path(chapter_id, recovery)
    path.write_text(json.dumps(project, indent=2, ensure_ascii=False), encoding="utf-8")
    return jsonify(ok=True, path=str(path), saved_at=project["saved_at"])


@app.get("/load_project")
def load_project():
    """Restore a saved session: returns {project, preview}.

    Query: chapter, recovery?(0/1). `preview` is the same payload /load_pdf returns
    (rebuilt from the persisted tiles, NO re-stitch) so the strip is shown again;
    it is null if the chapter has no stitched strip on disk.
    """
    chapter_id = _sanitize_chapter(request.args.get("chapter", ""))
    recovery = request.args.get("recovery") in ("1", "true", "yes")
    path = _project_path(chapter_id, recovery)
    if not path.exists():
        return jsonify(error=f"No saved {'recovery' if recovery else 'project'} "
                             f"for {chapter_id}."), 404
    project = json.loads(path.read_text(encoding="utf-8"))
    return jsonify(project=project, preview=_preview_payload(chapter_id))


@app.get("/project_status")
def project_status():
    """Report whether a chapter has a saved project / newer recovery (for the
    'restore unsaved work?' prompt). Returns {project:{exists,mtime},
    recovery:{exists,mtime}}."""
    chapter_id = _sanitize_chapter(request.args.get("chapter", ""))
    out = {}
    for key, recovery in (("project", False), ("recovery", True)):
        p = _project_path(chapter_id, recovery)
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
            _SETTINGS["split"] = max(280, min(1400, int(data["split"])))
        except (TypeError, ValueError):
            pass
    return jsonify(**_SETTINGS)


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

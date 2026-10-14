"""Stage 7 - Assemble the video, FAST.  Runs on: your laptop (ffmpeg).

A recap is just static frames with narration, so MoviePy's per-frame Python
render loop (compositing layers in Python for every single output frame) is pure
overhead. This stage skips it:

  1. PRECOMPOSE each beat's full 1920x1080 still ONCE with PIL - the frame fit on
     top of its own blurred + darkened COVER background (no raw black bars) - and
     save it as a temp PNG. All the compositing happens here, once per beat.
  2. ASSEMBLE with ffmpeg DIRECTLY (no MoviePy frame loop): each still is a timed
     image input; consecutive stills are joined with an ffmpeg `xfade` transition
     (style + duration come from the project's "animation" setting - see
     _resolve_animation() - default "fade" @ 0.35s, matching what was always
     hardcoded here before that setting existed; "cut" uses a plain `concat`
     instead, since a 0-duration xfade isn't a reliable no-op); the per-beat
     narration is delayed onto the timeline and mixed; a looped music bed is
     sidechain-DUCKED under the narration. Encoded at a constant quality chosen by the
     project's `video.quality` preset (see QUALITY) with libx264 at a fast preset. ffmpeg is REQUIRED - there
     is no slower fallback renderer; if it fails the stage fails with ffmpeg's message.

Everything is audio-driven and static (24fps): a beat holds for its narration
length (floored at MIN_BEAT, never padded for short lines; long audio just holds),
plus a short silent tail so lines don't jump-cut; the transition overlaps that tail
(no overlap at all for a "cut").

A line (beat) may carry MULTIPLE frames, read from the framer's
`work/<ch>/framer/mapping.json` (keyed by `order`); the per-frame flip/blur is
already baked into those PNGs, so s7 only ARRANGES them, per the line's `mode`:
  - "sequence": the line's on-screen time is split EVENLY across its frames -
    each becomes its own sub-still (frame 1, then 2, ...), all under the SINGLE
    narration clip (the audio is delayed onto the first sub-still only).
  - "together": all the line's frames are shown at once in one still - side by
    side (2) or a simple grid (2x2 ...) - held for the whole line like a normal
    beat.
A one-frame line behaves exactly as before. With no mapping (legacy manifests)
each beat falls back to its single `frame_image`.

Output: output/<chapter>/recap.mp4 (+ video_plan.json). Prints total render time.

PIL is imported eagerly (Pillow is a core dep). MoviePy is not used.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from config import ROOT, chapter_output_dir, chapter_work_dir, NO_CONSOLE
from manifest import Manifest, Panel
from atomic_io import read_json_with_backup_fallback
from gpu import ffmpeg_path, record_usage

# --- frame + template constants ------------------------------------------- #
W, H = 1920, 1080
FPS = 24
MIN_BEAT = 2.5         # floor on a beat's on-screen audio time (seconds)
NO_AUDIO_HOLD = 3.0    # a beat with no audio holds this long
LINE_GAP = 0.4         # silence between consecutive LINES' narration (tunable) so
                       # clips don't jump-cut into each other. Each line ends with
                       # this much trailing silence before the next line speaks;
                       # within a "sequence" line the sub-stills share one clip, so
                       # this gap only ever falls BETWEEN lines, never inside one.
CROSSFADE = 0.35       # DEFAULT crossfade length - overridden per-project by the
                       # "animation" setting (see _resolve_animation()); this is
                       # also the value an old project.json with no such setting
                       # falls back to, matching what was always hardcoded here.
FILL = 0.96            # frames (or a together-group) fill up to this share of the canvas
KEN_BURNS_MAX_ZOOM = 1.10  # peak scale for the alternating Ken Burns effect -
                           # always >= 1.0 so the zoompan crop never samples
                           # outside the still's own 1920x1080 canvas
KEN_BURNS_FILL = 0.88  # with Ken Burns on, frames fill less: at peak zoom the visible window is
                       # 1/KEN_BURNS_MAX_ZOOM (0.909) of the canvas, and 0.88 stays inside it
GROUP_GAP = 0.02       # gap between frames shown together, as a share of the canvas width
BG_BLUR = 40           # background gaussian blur radius (px)
BG_BRIGHTNESS = 0.40   # darken background to ~40% brightness
MUSIC_VOL = 0.06       # static music bed volume (low; ducked further under speech)
NARRATION_VOL = 1.15   # slight narration boost so speech sits above the bed
AUDIO_SR = 48000       # common sample rate for the audio graph
# Output size presets (project setting `video.quality`). x264 at a constant quality (crf): the
# stills barely change, so it spends bits only where the picture moves. A lower height and a
# blurred background (a looping video is full-screen motion, the most expensive thing in the
# frame) are what bring a chapter down to tens of megabytes.
QUALITY = {
    "tiny":     {"height": 720,  "crf": 38, "preset": "medium", "audio_kbps": 40,  "audio_channels": 1, "blur_bg": True},
    "small":    {"height": 720,  "crf": 33, "preset": "medium", "audio_kbps": 48,  "audio_channels": 1, "blur_bg": True},
    "standard": {"height": 1080, "crf": 29, "preset": "medium", "audio_kbps": 64,  "audio_channels": 1, "blur_bg": False},
    "high":     {"height": 1080, "crf": 23, "preset": "medium", "audio_kbps": 128, "audio_channels": 2, "blur_bg": False},
}
DEFAULT_QUALITY = "tiny"
BG_BLUR_DIVISOR = 10   # a blurred background is rendered at 1/10 size and scaled back up
FFMPEG_PROGRESS_EVERY_S = 3.0   # how often ffmpeg's progress line is passed on to the log
X264_PRESET = "veryfast"   # measured: the encoder is never the bottleneck (the filter graph is), so a
                           # fast preset costs no render speed; h264_nvenc was no faster and made files ~3x bigger

MUSIC_DIR = ROOT / "assets" / "music"
MUSIC_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".oga", ".aac", ".flac"}

# --- per-project media settings (framer's project system) ------------------ #
# A project's music/watermark/background are resolved by the framer (see
# tools/framer/projects.py: media_settings_snapshot()) into an ABSOLUTE-path
# snapshot written to <chapter work>/media_settings.json - the same pattern
# _load_mapping() below already uses for framer/mapping.json. Missing/garbled
# file -> every one of these falls back to today's behavior untouched.
_FONT_CANDIDATES = ["arialbd.ttf", "segoeuib.ttf", "arial.ttf", "segoeui.ttf"]
_FONTS_DIR = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
WATERMARK_STROKE = 2       # px outline so light text reads on light frames too


# --------------------------------------------------------------------------- #
# Planning (pure)
# --------------------------------------------------------------------------- #
def _audio_len(p: Panel) -> float:
    """The beat's real narration length, or a flat hold when it has no audio."""
    if p.audio and p.duration_s and Path(p.audio).exists():
        return float(p.duration_s)
    return NO_AUDIO_HOLD


def _load_mapping(chapter_id: str) -> dict[int, dict]:
    """Read the framer's mapping.json (the authoritative per-line plan with
    multiple frames + play mode), keyed by line `order`. Returns {} when there is
    no mapping (legacy manifests) so the render falls back to one frame per line.

    Tries mapping.json.bak (see atomic_io.py) before giving up on a corrupt/
    unreadable primary file - a mapping full of hand-drawn boxes silently
    degrading to "one frame per line" is itself a real loss of the user's
    boxing work, not just a render glitch, so it's worth one extra try before
    accepting that fallback."""
    path = chapter_work_dir(chapter_id) / "framer" / "mapping.json"
    if not path.exists():
        return {}
    try:
        data = read_json_with_backup_fallback(path)
    except Exception:  # noqa: BLE001 - a bad mapping must not sink the render
        return {}
    return {ln["order"]: ln for ln in data.get("lines", []) if "order" in ln}


def _load_media_settings(chapter_id: str) -> dict:
    """Read the framer's project media settings snapshot (music/watermark/
    background, absolute paths), written by tools/framer/projects.py. Mirrors
    _load_mapping() above: returns {} on any absence/failure, so run() falls
    back to today's global assets/music + blurred-crop behavior."""
    path = chapter_work_dir(chapter_id) / "media_settings.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a bad settings file must not sink the render
        return {}


# ffmpeg's `xfade` filter's own transition names - only the ones this app
# exposes as a style. "cut"/"fade" need no direction; "slide" reads
# `direction`; "zoom" only has one built-in xfade variant (zoomin) - ffmpeg
# has no matching "zoomout" transition, so that's the one "zoom" always maps
# to, regardless of the style being framed as generic "entry/exit" animation.
_SLIDE_XFADE = {"left": "slideleft", "right": "slideright", "up": "slideup", "down": "slidedown"}


def _zoom_filter(dur: float, zoom_in: bool, width: int, height: int) -> str:
    """One still's Ken Burns filter: `zoom_in` eases the scale from 1.0 to KEN_BURNS_MAX_ZOOM
    over the still's on-screen time, otherwise from there back to 1.0, and render alternates
    them per still. The picture is resampled with `perspective`, which takes fractional
    coordinates; ffmpeg's `zoompan` crops at whole pixels, so its zoom visibly shivers. The
    window stays centred and inside the canvas (zoom >= 1). `on` is the output-frame counter
    of this still's own filter instance, so the curve is a closed form of it and "zoom out"
    can start already zoomed. Smoothstep easing keeps the start and end of each move gentle."""
    last = max(1, round(dur * FPS) - 1)
    progress = f"(on/{last})"
    eased = f"({progress}*{progress}*(3-2*{progress}))"
    zoom = f"(1+{KEN_BURNS_MAX_ZOOM - 1.0:.4f}*{eased if zoom_in else f'(1-{eased})'})"
    x0, y0 = f"{width / 2}*(1-1/{zoom})", f"{height / 2}*(1-1/{zoom})"
    return (f"perspective=x0='{x0}':y0='{y0}':x1='{width}-{x0}':y1='{y0}':x2='{x0}':y2='{height}-{y0}':"
            f"x3='{width}-{x0}':y3='{height}-{y0}':sense=source:interpolation=linear:eval=frame")


def _resolve_animation(settings: dict) -> tuple[str, float, bool]:
    """(xfade transition name, or "cut" for a hard cut; crossfade seconds;
    alternating-Ken-Burns on/off) for this render, from the project's
    `animation` setting. Missing/garbled settings (or an old project.json/
    media_settings.json saved before a setting existed) fall back to EXACTLY
    today's fixed behavior - a plain fade crossfade at CROSSFADE seconds, Ken
    Burns off - so nothing changes unless someone explicitly picks something
    else in Project Settings. Ken Burns is independent of the transition
    style: it animates each still WHILE it's on screen (see
    _zoompan_filter()), the transition still plays between them as chosen."""
    cfg = settings.get("animation") or {}
    style = cfg.get("style")
    if style not in ("cut", "fade", "slide", "zoom"):
        style = "fade"
    duration = max(0.0, min(1.0, float(cfg.get("duration") or CROSSFADE)))
    ken_burns = bool(cfg.get("ken_burns"))
    if style == "cut":
        return "cut", 0.0, ken_burns
    if style == "slide":
        return _SLIDE_XFADE.get(cfg.get("direction"), "slideleft"), duration, ken_burns
    if style == "zoom":
        return "zoomin", duration, ken_burns
    return "fade", duration, ken_burns


def _line_frames(b: Panel, mapping: dict[int, dict]) -> tuple[list[str | None], str]:
    """Resolve a beat (line) to its (frame image paths, mode), reading the framer
    mapping by `order` and falling back to the beat's single `frame_image` when
    there is no mapping entry. Frames are already flipped/blurred on disk."""
    info = mapping.get(b.order)
    if info:
        frames = [f.get("image") for f in info.get("frames", []) if f.get("image")]
        mode = "together" if info.get("mode") == "together" else "sequence"
        if frames:
            return frames, mode
    return [b.frame_image], "sequence"


def _plan(beats: list[Panel], mapping: dict[int, dict], crossfade: float = CROSSFADE
          ) -> tuple[list[dict], float, list[dict]]:
    """Expand each beat (line) into one or more timeline sub-stills honouring its
    play mode, then time them. A "together" line is ONE still (frames gridded); a
    multi-frame "sequence" line is N sub-stills sharing the line's single
    narration (audio on the first sub-still only); a one-frame line is one still.

    `crossfade` is the project's chosen transition length (0 for a hard cut) -
    see _resolve_animation(). Consecutive items overlap on the timeline by
    exactly this much (0 = no overlap, a plain cut), which is also why this
    same value has to reach _build_filtergraph()/_build_cmd() unchanged: the
    overlap baked into these timestamps and the crossfade/concat the video
    filtergraph actually applies must always agree.

    Each item holds the still's source frame paths + layout, its duration, and
    its START on the final timeline. Returns (items, total_duration,
    line_summaries) - the summaries drive the per-line log.
    """
    tail = LINE_GAP + crossfade
    items: list[dict] = []
    lines: list[dict] = []
    for b in beats:
        al = _audio_len(b)
        audio = b.audio if (b.audio and Path(b.audio).exists()) else None
        frames, mode = _line_frames(b, mapping)
        if not frames:
            frames = [None]
        n = len(frames)

        if mode == "sequence" and n > 1:
            # Split the line's time evenly across its frames; the silent tail
            # rides on the last sub-still and the audio plays only over the
            # first. The N sub-stills crossfade with each other, which
            # shortens the line's wall-clock span by (N-1)*crossfade - that
            # would eat into the next line's narration and overlap it. Add
            # that time back to the line's budget so the inter-line silence
            # stays exactly LINE_GAP regardless of how many frames the line has.
            on_screen = round(max(al, MIN_BEAT), 3)
            line_total = round(on_screen + tail + (n - 1) * crossfade, 3)
            share = round(line_total / n, 3)
            for k, fr in enumerate(frames):
                dur = share if k < n - 1 else round(line_total - share * (n - 1), 3)
                items.append({
                    "order": b.order, "frames": [fr], "layout": "single",
                    "audio": audio if k == 0 else None,
                    "audio_len": round(al, 3) if k == 0 else 0.0, "dur": dur,
                })
            lines.append({"order": b.order, "mode": "sequence", "n_frames": n,
                          "stills": n, "frame_dur": share})
        else:
            # One still: a "together" grid (multi-frame) or a plain single frame.
            grid = mode == "together" and n > 1
            dur = round(max(al, MIN_BEAT) + tail, 3)
            items.append({
                "order": b.order, "frames": list(frames),
                "layout": "grid" if grid else "single",
                "audio": audio, "audio_len": round(al, 3), "dur": dur,
            })
            lines.append({"order": b.order, "mode": ("together" if grid else "single"),
                          "n_frames": n, "stills": 1, "frame_dur": None})

    n_items = len(items)
    for i, it in enumerate(items):
        it["start"] = round(sum(x["dur"] for x in items[:i]) - i * crossfade, 3)
    total = round(sum(it["dur"] for it in items) - max(0, n_items - 1) * crossfade, 3)
    return items, total, lines


def _music_file() -> Path | None:
    files = sorted(p for p in MUSIC_DIR.glob("*") if p.suffix.lower() in MUSIC_EXTS)
    return files[0] if files else None


def _resolve_music(settings: dict) -> Path | None:
    """The project's chosen music track, else today's global assets/music scan;
    None when the project opted out of a music bed."""
    if settings.get("music_off"):
        return None
    raw = settings.get("music")
    if raw and Path(raw).is_file():
        return Path(raw)
    return _music_file()


# --------------------------------------------------------------------------- #
# Precompose each beat's full-frame still ONCE (PIL)
# --------------------------------------------------------------------------- #
def _open_rgb(path: str | None):
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        from PIL import Image
        return Image.open(p).convert("RGB")
    except Exception:  # noqa: BLE001 - a bad frame must not sink the whole render
        return None


def _cover_crop(img, w: int, h: int):
    """Scale `img` to COVER a w x h box, then center-crop to exactly it."""
    from PIL import Image
    scale = max(w / img.width, h / img.height)
    r = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                    Image.LANCZOS)
    left, top = (r.width - w) // 2, (r.height - h) // 2
    return r.crop((left, top, left + w, top + h))


def _resolve_video_background(settings: dict) -> Path | None:
    """The project's VIDEO background file, if `background.kind == "video"` -
    played looped for the whole render as a base ffmpeg input, with the
    (transparent) frame stills overlaid on top - see run()/_build_filtergraph.
    This is a fundamentally different path from _load_background() below: a
    moving video can't be baked into a static PIL still, so it's never loaded
    as an image here, only resolved to a file path for ffmpeg itself to
    decode and loop. None when not configured, so run() falls back to
    _load_background()'s PIL-composited image/blur background untouched."""
    bg_cfg = settings.get("background")
    if not bg_cfg or bg_cfg.get("kind") != "video" or not bg_cfg.get("file"):
        return None
    p = Path(bg_cfg["file"])
    return p if p.exists() else None


def _load_background(settings: dict):
    """The project's custom background IMAGE, cover-cropped to 1920x1080 and
    dimmed (NOT blurred - a user-chosen background is deliberate art). Returns
    None when no custom background is set (including when it's a VIDEO
    background - see _resolve_video_background() above), so callers fall back
    to today's blurred-self-cover default untouched. Loaded once per render
    by run()."""
    bg_cfg = settings.get("background")
    if not bg_cfg or bg_cfg.get("kind") == "video" or not bg_cfg.get("file"):
        return None
    from PIL import Image, ImageEnhance
    p = Path(bg_cfg["file"])
    if not p.exists():
        return None
    try:
        img = Image.open(p).convert("RGB")
    except Exception:  # noqa: BLE001 - a bad background must not sink the render
        return None
    img = _cover_crop(img, W, H)
    return ImageEnhance.Brightness(img).enhance(float(bg_cfg.get("dim", 0.85)))


def _blurred_self_cover(img):
    """Today's default background: a blurred + darkened cover-crop of the frame
    itself. Extracted unchanged from the old _compose_still."""
    from PIL import ImageEnhance, ImageFilter
    bg = _cover_crop(img, W, H).filter(ImageFilter.GaussianBlur(BG_BLUR))
    return ImageEnhance.Brightness(bg).enhance(BG_BRIGHTNESS)


def _paste_background(base, bg, wm=None, transparent: bool = False):
    """Composite the background layer (custom `bg` or, when the caller passes
    None as `bg`, whatever the caller already resolved - see call sites) onto
    `base`, with the tiled watermark `wm` (if any) composited onto the
    BACKGROUND FIRST. Callers paste the frame crop(s) on top of `base`
    afterward, so a crop always covers/hides the watermark under it - the
    watermark only ever shows in the background area around/between frames,
    never on top of the comic art itself.

    `transparent=True` (a VIDEO background is playing behind this still - see
    run()/_resolve_video_background) skips pasting any image at all: `bg` is
    ignored and `base` (already an RGBA canvas, see _compose_still) is left
    fully transparent outside the watermark, so the moving video underneath
    shows through once this still is `overlay`'d onto it in the ffmpeg
    filtergraph. Uses `Image.alpha_composite`, NOT `base.paste(wm, box, wm)` -
    tried that first, but pasting an RGBA image using ITS OWN alpha as the
    mask double-applies that alpha (once as the "source" pixels being
    blended, once again as the blend weight itself), which - verified with a
    quick manual check - darkens the color and under-reports the resulting
    opacity on a fully-transparent destination. `alpha_composite` implements
    real "over" compositing and doesn't have that problem. Returns `base`
    (a NEW object in this branch, unlike the plain `.paste()` below) -
    callers must use the return value, not assume in-place mutation."""
    from PIL import Image

    if transparent:
        if wm is not None:
            base = Image.alpha_composite(base, wm)
        return base

    if wm is not None:
        bg = Image.alpha_composite(bg.convert("RGBA"), wm).convert("RGB")
    base.paste(bg, (0, 0))
    return base


def _row_splits(n: int):
    """Every way to cut n frames, in order, into consecutive rows."""
    for cuts in itertools.product((False, True), repeat=n - 1):
        rows, start = [], 0
        for i, cut in enumerate(cuts, start=1):
            if cut:
                rows.append(range(start, i))
                start = i
        yield rows + [range(start, n)]


def _arrange(aspects: list[float], fit_w: float, fit_h: float, gap: float) -> list[tuple[float, float, float, float]]:
    """(x, y, w, h) of each frame, given its width/height ratio, packed into a fit_w x fit_h
    box as rows of one common height. The split into rows with the tallest rows (hence the
    largest frames) wins, so two tall frames sit side by side and two wide ones stack."""
    def row_height(rows) -> float:
        return min((fit_h - gap * (len(rows) - 1)) / len(rows),
                   *((fit_w - gap * (len(row) - 1)) / sum(aspects[i] for i in row) for row in rows))

    best_rows = max(_row_splits(len(aspects)), key=row_height)
    height = row_height(best_rows)
    y = (fit_h - (height * len(best_rows) + gap * (len(best_rows) - 1))) / 2
    boxes = [None] * len(aspects)
    for row in best_rows:
        x = (fit_w - (height * sum(aspects[i] for i in row) + gap * (len(row) - 1))) / 2
        for i in row:
            boxes[i] = (x, y, height * aspects[i], height)
            x += height * aspects[i] + gap
        y += height + gap
    return boxes


def _compose_still(frame_paths: list[str | None], bg=None, wm=None, transparent: bool = False,
                   fill: float = FILL):
    """Build the full 1920x1080 still for a beat: its frames (one, or several shown
    together) packed as large as `fill` of the canvas allows, over `bg` (a project's custom
    background) if given, else a blurred + darkened cover of the first frame - UNLESS
    `transparent` is set (a video background - see _paste_background), in which case the
    background area is left transparent instead (an RGBA image is returned, not RGB). `wm`
    (a watermark RGBA layer), if given, is composited onto the background/transparent area
    ONLY, before the frames go on top - see _paste_background."""
    from PIL import Image

    base = (Image.new("RGBA", (W, H), (0, 0, 0, 0)) if transparent
            else Image.new("RGB", (W, H), (12, 12, 12)))
    imgs = [img for img in (_open_rgb(path) for path in frame_paths) if img is not None]
    if not imgs:
        return base

    if bg is None and not transparent:
        bg = _blurred_self_cover(imgs[0])
    base = _paste_background(base, bg, wm, transparent)

    fit_w, fit_h = W * fill, H * fill
    boxes = _arrange([img.width / img.height for img in imgs], fit_w, fit_h, GROUP_GAP * W)
    for img, (x, y, w, h) in zip(imgs, boxes):
        size = (max(1, round(w)), max(1, round(h)))
        base.paste(img.resize(size, Image.LANCZOS), (round((W - fit_w) / 2 + x), round((H - fit_h) / 2 + y)))
    return base


# --------------------------------------------------------------------------- #
# Tiled watermark - built ONCE per render as a single 1920x1080 RGBA layer,
# then alpha-composited onto every precomposed still (see _precompose). PIL,
# not an ffmpeg filter: it costs one composite per BEAT (tens) rather than per
# output frame (~24fps x total_s).
# --------------------------------------------------------------------------- #
def _find_font(px: int):
    from PIL import ImageFont
    for name in _FONT_CANDIDATES:
        p = _FONTS_DIR / name
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), px)
            except Exception:  # noqa: BLE001
                continue
    try:
        return ImageFont.load_default(size=px)
    except TypeError:  # older Pillow: load_default() takes no `size`
        return ImageFont.load_default()


def _make_stamp(cfg: dict):
    """One watermark sprite (RGBA) scaled so its width is cfg['size'] * W - a
    logo image, or rendered text solved to that same target width."""
    from PIL import Image, ImageDraw

    target_w = max(16, round(float(cfg.get("size", 0.12)) * W))

    if cfg.get("type") == "image" and cfg.get("file"):
        p = Path(cfg["file"])
        if p.is_file():
            try:
                img = Image.open(p).convert("RGBA")
                h = max(1, round(img.height * target_w / img.width))
                return img.resize((target_w, h), Image.LANCZOS)
            except Exception:  # noqa: BLE001 - fall through to text/None
                pass
        return None

    text = (cfg.get("text") or "").strip()
    if not text:
        return None
    # Solve the font size so the rendered text width matches target_w.
    size = max(8, round(target_w / max(1, len(text)) * 1.8))
    font = _find_font(size)
    tmp = Image.new("RGBA", (10, 10))
    bbox = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font, stroke_width=WATERMARK_STROKE)
    w = max(1, bbox[2] - bbox[0])
    if w != target_w:
        size = max(8, round(size * target_w / w))
        font = _find_font(size)
        bbox = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=font, stroke_width=WATERMARK_STROKE)
    pad = WATERMARK_STROKE + 2
    tw, th = bbox[2] - bbox[0] + pad * 2, bbox[3] - bbox[1] + pad * 2
    stamp = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    ImageDraw.Draw(stamp).text((pad - bbox[0], pad - bbox[1]), text, font=font,
                               fill=(255, 255, 255, 255),
                               stroke_width=WATERMARK_STROKE, stroke_fill=(0, 0, 0, 255))
    return stamp


def _build_watermark_layer(settings: dict):
    """One 1920x1080 RGBA layer, tiled with the project's stamp at low opacity,
    built once per render. None when the project has no watermark enabled."""
    from PIL import Image

    cfg = settings.get("watermark")
    if not cfg:
        return None
    stamp = _make_stamp(cfg)
    if stamp is None:
        return None

    opacity = max(0.0, min(1.0, float(cfg.get("opacity", 0.12))))
    alpha = stamp.getchannel("A").point(lambda v: int(v * opacity))
    stamp.putalpha(alpha)

    spacing = max(0.0, float(cfg.get("spacing", 1.0)))
    step_x = max(1, round(stamp.width * (1 + spacing)))
    step_y = max(1, round(stamp.height * (1 + spacing)))

    # Tile onto an oversized canvas so a rotation never leaves a bare corner,
    # then rotate and center-crop back to the frame size.
    big_w, big_h = W * 2, H * 2
    canvas = Image.new("RGBA", (big_w, big_h), (0, 0, 0, 0))
    row = 0
    for y in range(-step_y, big_h + step_y, step_y):
        x0 = -step_x + (step_x // 2 if row % 2 else 0)
        for x in range(x0, big_w + step_x, step_x):
            canvas.alpha_composite(stamp, (x, y))
        row += 1

    angle = max(-90, min(90, float(cfg.get("angle", 0))))
    if angle:
        canvas = canvas.rotate(angle, expand=False, resample=Image.BICUBIC)
    left, top = (big_w - W) // 2, (big_h - H) // 2
    return canvas.crop((left, top, left + W, top + H))


def _precompose(items: list[dict], stills_dir: Path, wm=None, bg=None,
                transparent: bool = False, fill: float = FILL, out_height: int = H) -> None:
    """Render every item's still PNG into stills_dir and record its path on the
    item (key "still"). All the per-frame compositing work happens here, once.
    A "grid" item shows its frames together; everything else is one frame. `fill` is the
    share of the canvas the frames may cover (less with Ken Burns, which crops the edges).
    `bg` (a PIL image) replaces the default blurred self-cover when given; `wm`
    (a PIL RGBA layer), when given, is composited onto the BACKGROUND layer
    only (see _compose_still) - frame crops are pasted on
    top of that afterward, so they always cover/hide the watermark under them.
    `transparent=True` (a video background - see run()) saves RGBA PNGs with
    the background area left transparent instead, for ffmpeg to `overlay`
    onto the looped background video."""
    stills_dir.mkdir(parents=True, exist_ok=True)
    for i, it in enumerate(items):
        out = stills_dir / f"beat_{i:04d}.png"
        still = _compose_still(it["frames"], bg, wm, transparent, fill)
        if out_height != H:
            from PIL import Image
            still = still.resize((round(out_height * W / H / 2) * 2, out_height), Image.LANCZOS)
        still.save(out)
        it["still"] = str(out)


# --------------------------------------------------------------------------- #
# ffmpeg assembly (direct - no MoviePy frame loop)
# --------------------------------------------------------------------------- #
def _build_filtergraph(items: list[dict], music_in: int | None, total: float,
                       transition: str = "fade", crossfade: float = CROSSFADE,
                       ken_burns: bool = False, bg_in: int | None = None,
                       bg_dim: float = 0.85, out_height: int = H, blur_bg: bool = False
                       ) -> tuple[str, str, str | None]:
    """Build the ffmpeg filter_complex for the video transition chain + audio
    mix. Image inputs are 0..n-1; narration audio inputs follow; the video
    background (if any) is at index `bg_in`; music (if any) is at index
    `music_in`. `transition` is an ffmpeg `xfade` transition name (e.g.
    "fade", "slideleft", "zoomin") - or "cut" for a hard cut, which uses
    `concat` instead of `xfade` (a 0-duration xfade isn't a reliable no-op).
    `crossfade` must be the SAME value _plan() timed these items' overlaps
    with - see _resolve_animation()/run(). Returns (filter_complex,
    video_label, audio_label|None).

    `ken_burns` applies a per-still zoom (see _zoom_filter) BEFORE the
    transition chain, alternating zoom direction by each item's position.

    `bg_in` (set only when a video background is configured - see
    _resolve_video_background/run()) changes what the still chain normalizes
    to: `yuva420p` (keeps alpha) instead of `yuv420p`, since with a video
    background the stills were precomposed with a TRANSPARENT background
    (see _precompose(..., transparent=True)) - the transition chain's output
    is then `overlay`'d onto the looped/dimmed background video as this
    function's last step, and THAT combined result becomes `vlabel`. `blur_bg` renders that
    background blurred (cheap to encode); `out_height` is the size the whole video chain runs at
    (stills are scaled down first, so a 720p render does well under half the work of 1080p)."""
    n = len(items)
    out_w = round(out_height * W / H / 2) * 2
    parts: list[str] = []
    fg_pix_fmt = "yuva420p" if bg_in is not None else "yuv420p"
    fg_out = "[vout_fg]" if bg_in is not None else "[vout]"

    # --- video: normalize each still (+ optional Ken Burns), then chain transitions ---
    for i in range(n):
        chain = f"[{i}:v]fps={FPS}"
        # the stills are already composed at the output size (see _precompose), so there is no
        # per-frame scale here; converting to planar YUV first halves what Ken Burns has to warp
        chain += f",format={fg_pix_fmt}"
        if ken_burns:
            chain += "," + _zoom_filter(items[i]["dur"], (i % 2 == 0), out_w, out_height)
        chain += f",setsar=1[v{i}]"
        parts.append(chain)
    if n == 1:
        vlabel = "[v0]"
    elif transition == "cut" or crossfade <= 0:
        vparts = "".join(f"[v{i}]" for i in range(n))
        parts.append(f"{vparts}concat=n={n}:v=1:a=0{fg_out}")
        vlabel = fg_out
    else:
        prev, acc = "[v0]", items[0]["dur"]
        for j in range(1, n):
            off = acc - crossfade
            out = fg_out if j == n - 1 else f"[x{j}]"
            parts.append(f"{prev}[v{j}]xfade=transition={transition}:"
                         f"duration={crossfade:.3f}:offset={off:.3f}{out}")
            acc += items[j]["dur"] - crossfade
            prev = out
        vlabel = fg_out

    # --- video background: loop it under the (transparent) foreground track ---
    if bg_in is not None:
        brightness = max(-1.0, min(1.0, float(bg_dim) - 1.0))
        soften = (f"scale={out_w // BG_BLUR_DIVISOR}:{out_height // BG_BLUR_DIVISOR},"
                  f"scale={out_w}:{out_height}:flags=bilinear," if blur_bg else "")
        parts.append(
            f"[{bg_in}:v]scale={out_w}:{out_height}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_height},{soften}fps={FPS},eq=brightness={brightness:.3f},"
            f"format=yuv420p,setsar=1[bgv]"
        )
        parts.append(f"[bgv]{vlabel}overlay=x=0:y=0:format=auto,format=yuv420p[vout]")
        vlabel = "[vout]"

    # --- audio: delay each narration onto the timeline, mix, duck music ---
    # Narration inputs were appended in order for the beats that have audio.
    narr_labels: list[str] = []
    a_in = n  # first audio input index
    for it in items:
        if not it["audio"]:
            continue
        delay = max(0, int(round(it["start"] * 1000)))
        lab = f"[na{a_in}]"
        parts.append(f"[{a_in}:a]aresample={AUDIO_SR},aformat=channel_layouts=stereo,"
                     f"volume={NARRATION_VOL},adelay={delay}:all=1{lab}")
        narr_labels.append(lab)
        a_in += 1

    narr = None
    if len(narr_labels) == 1:
        parts.append(f"{narr_labels[0]}anull[narr]")
        narr = "[narr]"
    elif len(narr_labels) > 1:
        parts.append("".join(narr_labels)
                     + f"amix=inputs={len(narr_labels)}:normalize=0[narr]")
        narr = "[narr]"

    music = None
    if music_in is not None:
        parts.append(f"[{music_in}:a]aresample={AUDIO_SR},"
                     f"aformat=channel_layouts=stereo,volume={MUSIC_VOL}[music]")
        music = "[music]"

    if narr and music:
        # Sidechain-duck the music with the narration, then sum them.
        parts.append(f"{narr}asplit=2[narr_main][narr_key]")
        parts.append("[music][narr_key]sidechaincompress="
                     "threshold=0.02:ratio=8:attack=5:release=250[mduck]")
        parts.append("[narr_main][mduck]amix=inputs=2:normalize=0[aout]")
        alabel = "[aout]"
    elif narr:
        alabel = narr
    elif music:
        alabel = music
    else:
        alabel = None

    return ";".join(parts), vlabel, alabel


def _build_cmd(ffmpeg: str, items: list[dict], music_path: Path | None,
               total: float, out: Path, quality: dict,
               transition: str = "fade", crossfade: float = CROSSFADE,
               ken_burns: bool = False, video_bg: Path | None = None,
               bg_dim: float = 0.85) -> list[str]:
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats"]
    # Image inputs (timed, looped to their duration).
    for it in items:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{it['dur']:.3f}",
                "-i", it["still"]]
    # Narration audio inputs, in beat order (only beats with audio).
    for it in items:
        if it["audio"]:
            cmd += ["-i", it["audio"]]
    next_in = len(items) + sum(1 for it in items if it["audio"])
    # Video background (looped indefinitely - the final -t total below trims
    # everything, including this, to the render's actual length).
    bg_in = None
    if video_bg is not None:
        bg_in = next_in
        cmd += ["-stream_loop", "-1", "-i", str(video_bg)]
        next_in += 1
    # Music input (looped) last.
    music_in = None
    if music_path is not None:
        music_in = next_in
        cmd += ["-stream_loop", "-1", "-i", str(music_path)]

    fc, vlabel, alabel = _build_filtergraph(items, music_in, total, transition, crossfade,
                                            ken_burns, bg_in, bg_dim, quality["height"], quality["blur_bg"])
    # the graph goes through a file: inline, a long chapter overflows Windows' command-line limit
    script = Path(items[0]["still"]).with_name("filtergraph.txt")
    script.write_text(fc, encoding="utf-8")
    cmd += ["-/filter_complex", str(script), "-map", vlabel]
    if alabel is not None:
        cmd += ["-map", alabel, "-c:a", "aac", "-b:a", f"{quality['audio_kbps']}k", "-ac", str(quality["audio_channels"])]

    cmd += ["-c:v", "libx264", "-preset", X264_PRESET, "-crf", str(quality["crf"]), "-movflags", "+faststart"]
    cmd += ["-pix_fmt", "yuv420p", "-r", str(FPS), "-t", f"{total:.3f}", str(out)]
    return cmd


_STAT = {k: re.compile(rf"\b{k}=\s*(\S+)") for k in ("frame", "fps", "time", "speed", "bitrate")}


def _clock(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}h{s % 3600 // 60:02d}m{s % 60:02d}s" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def _progress_line(stats_line: str, total_frames: int, total_s: float, started: float) -> str:
    """One readable progress line from an ffmpeg `-stats` line: how far, how fast, how long left.
    Keeps the `frame=` / `time=` fields the framer's progress bar parses."""
    got = {k: (m.group(1) if (m := rx.search(stats_line)) else "?") for k, rx in _STAT.items()}
    try:
        h, m_, sec = got["time"].split(":")
        done_s = int(h) * 3600 + int(m_) * 60 + float(sec)
    except ValueError:
        done_s = 0.0
    elapsed = time.monotonic() - started
    pct = min(100.0, 100.0 * done_s / total_s) if total_s else 0.0
    eta = elapsed * (total_s - done_s) / done_s if done_s > 0 else 0.0
    return (f"  render {pct:3.0f}%  frame={got['frame']}/{total_frames}  fps={got['fps']}  "
            f"time={got['time']}  speed={got['speed']} realtime  bitrate={got['bitrate']}  "
            f"elapsed {_clock(elapsed)}  eta {_clock(eta) if done_s > 0 else '?'}")


def _run_ffmpeg(cmd: list[str], total_frames: int, total_s: float) -> tuple[int, list[str]]:
    """Run ffmpeg, logging its progress (every few seconds: percent, frames, encode fps, speed vs
    realtime, ETA) and any error as they appear. stderr must be piped explicitly: with no console,
    an inherited one goes nowhere. Returns (exit code, the non-progress lines it printed)."""
    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", **NO_CONSOLE)
    errors, shown = [], 0.0
    for line in proc.stderr:
        line = line.strip()
        if line.startswith("frame="):
            if time.monotonic() - shown >= FFMPEG_PROGRESS_EVERY_S:
                shown = time.monotonic()
                print(_progress_line(line, total_frames, total_s, started), flush=True)
        elif line:
            errors.append(line)
            print(f"  {line}", flush=True)
    return proc.wait(), errors


def _require_ffmpeg() -> str:
    """The ffmpeg executable, or a clear error: ffmpeg is mandatory for this stage."""
    exe = ffmpeg_path()
    if not (Path(exe).is_file() or shutil.which(exe)):
        raise RuntimeError("ffmpeg was not found (looked for vendor/ffmpeg/bin/ffmpeg.exe, then PATH). "
                           "Run setup.bat to install it - video rendering requires ffmpeg.")
    return exe


def _ffmpeg_version(ffmpeg: str) -> str:
    try:
        out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=15, **NO_CONSOLE).stdout
        return out.splitlines()[0] if out else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _assemble_ffmpeg(items: list[dict], music_path: Path | None, total: float, out: Path,
                     quality: dict, transition: str = "fade", crossfade: float = CROSSFADE,
                     ken_burns: bool = False, video_bg: Path | None = None,
                     bg_dim: float = 0.85) -> str:
    """Render with ffmpeg directly, logging exactly what is being run. Returns the codec used;
    raises if ffmpeg fails (there is no fallback renderer)."""
    ffmpeg = _require_ffmpeg()
    total_frames = int(round(total * FPS))
    cmd = _build_cmd(ffmpeg, items, music_path, total, out, quality, transition, crossfade,
                     ken_burns, video_bg, bg_dim)
    print(f"  ffmpeg: {ffmpeg}", flush=True)
    print(f"          {_ffmpeg_version(ffmpeg)}", flush=True)
    print(f"  encoder: libx264 (CPU), preset {X264_PRESET}, crf {quality['crf']}, "
          f"audio aac {quality['audio_kbps']}k x{quality['audio_channels']}", flush=True)
    print(f"  output: {round(quality['height'] * W / H / 2) * 2}x{quality['height']} @ {FPS}fps, "
          f"{total_frames} frames, {total:.1f}s -> {out}", flush=True)
    print(f"  filters: {len(items)} stills, transition={transition} @ {crossfade:.2f}s, "
          f"ken_burns={'on (perspective, linear)' if ken_burns else 'off'}, "
          f"video_background={'on' if video_bg else 'off'}, music={'on' if music_path else 'off'}, "
          f"narration clips={sum(1 for it in items if it['audio'])}", flush=True)
    print("  command: " + " ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    started = time.monotonic()
    returncode, errors = _run_ffmpeg(cmd, total_frames, total)
    took = time.monotonic() - started
    if returncode != 0:
        raise RuntimeError(f"ffmpeg exited {returncode}: {errors[-1] if errors else 'no message'}")
    print(f"  ffmpeg finished: {total / max(took, 0.001):.2f}x realtime, "
          f"{total_frames / max(took, 0.001):.0f} fps average, {_clock(took)} for {_clock(total)} of video",
          flush=True)
    return "libx264"


def _resolve_quality(settings: dict) -> tuple[str, dict]:
    """(name, preset) of the project's output quality; anything unknown falls back to the default."""
    name = settings.get("quality")
    name = name if name in QUALITY else DEFAULT_QUALITY
    return name, QUALITY[name]


# --------------------------------------------------------------------------- #
def run(m: Manifest) -> Manifest:
    out_dir = chapter_output_dir(m.chapter_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    beats = m.kept_panels()  # the matched narration beats (STAGE B)
    mapping = _load_mapping(m.chapter_id)  # framer's multi-frame + mode plan
    settings = _load_media_settings(m.chapter_id)  # framer's per-project media settings
    transition, crossfade, ken_burns = _resolve_animation(settings)
    quality_name, quality = _resolve_quality(settings)
    items, total, lines = _plan(beats, mapping, crossfade)
    music_path = _resolve_music(settings)
    wm_cfg = settings.get("watermark")
    print(f"  [debug] media_settings.json watermark cfg: {wm_cfg!r}")
    watermark = _build_watermark_layer(settings)
    print(f"  [debug] watermark: enabled={bool(wm_cfg and wm_cfg.get('enabled'))}, "
          f"layer_built={watermark is not None}, compositing onto {len(items)} still(s)")
    # A video background (see _resolve_video_background) and a static image/
    # blur background (see _load_background) are mutually exclusive - the
    # project's `background.mode`/`kind` is either "video", "image", or
    # "blur". When it's video, stills are precomposed TRANSPARENT instead
    # (see _precompose(transparent=...)) and the video is `overlay`'d under
    # them in the ffmpeg filtergraph - never both at once.
    video_bg = _resolve_video_background(settings)
    bg_dim = float((settings.get("background") or {}).get("dim", 0.85))
    background = None if video_bg is not None else _load_background(settings)

    # Per-line report so multi-frame handling can be verified at a glance.
    for ln in lines:
        if ln["mode"] == "sequence" and ln["n_frames"] > 1:
            extra = f" -> {ln['stills']} sub-stills @ ~{ln['frame_dur']:.2f}s each"
        elif ln["mode"] == "together":
            extra = f" -> 1 grid still ({ln['n_frames']} frames at once)"
        else:
            extra = " -> 1 still"
        print(f"  line {ln['order']}: {ln['n_frames']} frame(s), mode={ln['mode']}{extra}")

    for old in ("recap.mp4", "video_plan.json"):
        (out_dir / old).unlink(missing_ok=True)
    (out_dir / "video_plan.json").write_text(
        json.dumps({
            "chapter_id": m.chapter_id, "size": [W, H], "fps": FPS,
            "music": music_path.name if music_path else None,
            "watermark": bool(watermark), "background": bool(background),
            "video_background": bool(video_bg), "ken_burns": ken_burns,
            "animation": {"transition": transition, "crossfade_s": crossfade},
            "total_s": total, "beats": items,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if not items:
        print("  no panels to render; wrote video_plan.json only")
        m.stage = "video"
        return m

    out = out_dir / "recap.mp4"
    stills_dir = out_dir / "_stills"
    t0 = time.perf_counter()
    try:
        _precompose(items, stills_dir, watermark, background, transparent=(video_bg is not None),
                    fill=KEN_BURNS_FILL if ken_burns else FILL, out_height=quality["height"])
        anim_desc = "cut (no transition)" if transition == "cut" else f"{transition} @ {crossfade:.2f}s"
        if ken_burns:
            anim_desc += " + alternating Ken Burns"
        print(f"  precomposed {len(items)} stills ({total:.1f}s timeline), transition={anim_desc}"
              f"{' + watermark' if watermark else ''}{' + custom background' if background else ''}"
              f"{' + video background' if video_bg else ''}")
        t_pre = time.perf_counter() - t0
        print(f"  precompose took {t_pre:.1f}s ({len(items) / max(t_pre, 0.001):.1f} stills/s)", flush=True)
        codec = _assemble_ffmpeg(items, music_path, total, out, quality, transition, crossfade,
                                 ken_burns, video_bg, bg_dim)
        path = "ffmpeg"
    finally:
        shutil.rmtree(stills_dir, ignore_errors=True)

    dt = time.perf_counter() - t0
    record_usage("video", codec, False)
    if music_path:
        print(f"  music bed: {music_path.name} @ vol {MUSIC_VOL} (ducked)")
    print(f"  rendered {len(lines)} lines / {len(items)} stills ({total:.1f}s, "
          f"{FPS}fps, {quality_name} {quality['height']}p, {codec} via {path}, "
          f"{out.stat().st_size / 1e6:.1f} MB) in {dt:.1f}s -> {out}")
    m.stage = "video"
    return m

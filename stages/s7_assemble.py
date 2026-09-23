"""Stage 7 - Assemble the video, FAST.  Runs on: your laptop (NVENC/ffmpeg).

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
     sidechain-DUCKED under the narration. Encoded with h264_nvenc when
     available, else libx264.
  3. MoviePy is kept only as a FALLBACK if the ffmpeg path fails (it reuses the
     same precomposed stills, so it never re-composites in Python either) -
     it only distinguishes cut vs. a plain crossfade, see _assemble_moviepy().

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

PIL is imported eagerly (Pillow is a core dep); MoviePy is imported LAZILY inside
the fallback so the orchestrator can import this module without moviepy installed.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

from config import ROOT, chapter_output_dir, chapter_work_dir
from manifest import Manifest, Panel

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
CROP_FRAC_H = 0.85     # frame fit to this fraction of HEIGHT ...
CROP_FRAC_W = 0.92     # ... and capped at this fraction of WIDTH (wide panels)
BG_BLUR = 40           # background gaussian blur radius (px)
BG_BRIGHTNESS = 0.40   # darken background to ~40% brightness
MUSIC_VOL = 0.06       # static music bed volume (low; ducked further under speech)
NARRATION_VOL = 1.15   # slight narration boost so speech sits above the bed
AUDIO_SR = 48000       # common sample rate for the audio graph

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
    no mapping (legacy manifests) so the render falls back to one frame per line."""
    path = chapter_work_dir(chapter_id) / "framer" / "mapping.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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


def _resolve_animation(settings: dict) -> tuple[str, float]:
    """(xfade transition name, or "cut" for a hard cut; crossfade seconds) for
    this render, from the project's `animation` setting. Missing/garbled
    settings (or an old project.json/media_settings.json saved before this
    setting existed) fall back to EXACTLY today's fixed behavior - a plain
    fade crossfade at CROSSFADE seconds - so nothing changes unless someone
    explicitly picks something else in Project Settings."""
    cfg = settings.get("animation") or {}
    style = cfg.get("style")
    if style not in ("cut", "fade", "slide", "zoom"):
        style = "fade"
    duration = max(0.0, min(1.0, float(cfg.get("duration") or CROSSFADE)))
    if style == "cut":
        return "cut", 0.0
    if style == "slide":
        return _SLIDE_XFADE.get(cfg.get("direction"), "slideleft"), duration
    if style == "zoom":
        return "zoomin", duration
    return "fade", duration


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
    """The project's chosen music track, else today's global assets/music scan."""
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


def _load_background(settings: dict):
    """The project's custom background image, cover-cropped to 1920x1080 and
    dimmed (NOT blurred - a user-chosen background is deliberate art). Returns
    None when no custom background is set, so callers fall back to today's
    blurred-self-cover default untouched. Loaded once per render by run()."""
    bg_cfg = settings.get("background")
    if not bg_cfg or not bg_cfg.get("file"):
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
    itself. Extracted unchanged from the old _compose_still/_compose_grid_still."""
    from PIL import ImageEnhance, ImageFilter
    bg = _cover_crop(img, W, H).filter(ImageFilter.GaussianBlur(BG_BLUR))
    return ImageEnhance.Brightness(bg).enhance(BG_BRIGHTNESS)


def _paste_background(base, bg, wm=None):
    """Composite the background layer (custom `bg` or, when the caller passes
    None as `bg`, whatever the caller already resolved - see call sites) onto
    `base`, with the tiled watermark `wm` (if any) composited onto the
    BACKGROUND FIRST. Callers paste the frame crop(s) on top of `base`
    afterward, so a crop always covers/hides the watermark under it - the
    watermark only ever shows in the background area around/between frames,
    never on top of the comic art itself."""
    from PIL import Image

    if wm is not None:
        bg = Image.alpha_composite(bg.convert("RGBA"), wm).convert("RGB")
    base.paste(bg, (0, 0))


def _compose_still(frame_path: str | None, bg=None, wm=None):
    """Build the full 1920x1080 still for a beat: the frame fit to ~85%H/~92%W,
    centered over `bg` (a project's custom background) if given, else a blurred
    + darkened cover of itself as before. `wm` (a watermark RGBA layer), if
    given, is composited onto the background ONLY, before the frame crop goes
    on top - see _paste_background. Returns a PIL RGB image."""
    from PIL import Image

    base = Image.new("RGB", (W, H), (12, 12, 12))
    img = _open_rgb(frame_path)
    if img is None:
        return base

    _paste_background(base, bg if bg is not None else _blurred_self_cover(img), wm)

    # Foreground: fit within 85%H / 92%W (whichever is tighter), centered.
    fscale = min((CROP_FRAC_H * H) / img.height, (CROP_FRAC_W * W) / img.width)
    fw, fh = max(1, round(img.width * fscale)), max(1, round(img.height * fscale))
    fg = img.resize((fw, fh), Image.LANCZOS)
    base.paste(fg, ((W - fw) // 2, (H - fh) // 2))
    return base


def _compose_grid_still(frame_paths: list[str | None], bg=None, wm=None):
    """Build the full 1920x1080 still for a "together" line: every frame fit into
    its cell of a simple grid (2 -> side by side, 3-4 -> 2x2, etc.), over `bg`
    (a project's custom background) if given, else a shared blurred + darkened
    cover of the first frame as before. `wm`, if given, is composited onto the
    background ONLY, before any frame crop goes on top - see _compose_still.
    Returns a PIL RGB image."""
    from PIL import Image

    imgs = [im for im in (_open_rgb(p) for p in frame_paths) if im is not None]
    if not imgs:
        return Image.new("RGB", (W, H), (12, 12, 12))
    if len(imgs) == 1:
        return _compose_still(frame_paths[0], bg, wm)

    base = Image.new("RGB", (W, H), (12, 12, 12))
    _paste_background(base, bg if bg is not None else _blurred_self_cover(imgs[0]), wm)

    # Foreground grid: cols ~ sqrt(N), each frame fit within ~92% of its cell.
    n = len(imgs)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    cell_w, cell_h = W / cols, H / rows
    for idx, img in enumerate(imgs):
        r, c = divmod(idx, cols)
        fscale = min((0.92 * cell_w) / img.width, (0.92 * cell_h) / img.height)
        fw, fh = max(1, round(img.width * fscale)), max(1, round(img.height * fscale))
        fg = img.resize((fw, fh), Image.LANCZOS)
        cx, cy = (c + 0.5) * cell_w, (r + 0.5) * cell_h
        base.paste(fg, (round(cx - fw / 2), round(cy - fh / 2)))
    return base


# --------------------------------------------------------------------------- #
# Tiled watermark - built ONCE per render as a single 1920x1080 RGBA layer,
# then alpha-composited onto every precomposed still (see _precompose). PIL,
# not an ffmpeg filter: the MoviePy fallback (_assemble_moviepy) reuses these
# same stills, so both render paths get the watermark for free from one code
# path, and it costs one composite per BEAT (tens) rather than per output
# frame (~24fps x total_s).
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


def _precompose(items: list[dict], stills_dir: Path, wm=None, bg=None) -> None:
    """Render every item's still PNG into stills_dir and record its path on the
    item (key "still"). All the per-frame compositing work happens here, once.
    A "grid" item lays its frames out together; everything else is one frame.
    `bg` (a PIL image) replaces the default blurred self-cover when given; `wm`
    (a PIL RGBA layer), when given, is composited onto the BACKGROUND layer
    only (see _compose_still/_compose_grid_still) - frame crops are pasted on
    top of that afterward, so they always cover/hide the watermark under them."""
    stills_dir.mkdir(parents=True, exist_ok=True)
    for i, it in enumerate(items):
        out = stills_dir / f"beat_{i:04d}.png"
        if it["layout"] == "grid":
            still = _compose_grid_still(it["frames"], bg, wm)
        else:
            still = _compose_still(it["frames"][0], bg, wm)
        still.save(out)
        it["still"] = str(out)


# --------------------------------------------------------------------------- #
# ffmpeg assembly (direct - no MoviePy frame loop)
# --------------------------------------------------------------------------- #
def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _has_nvenc(ffmpeg: str) -> bool:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=20)
        return "h264_nvenc" in (out.stdout or "")
    except Exception:  # noqa: BLE001
        return False


def _build_filtergraph(items: list[dict], music_in: int | None, total: float,
                       transition: str = "fade", crossfade: float = CROSSFADE
                       ) -> tuple[str, str, str | None]:
    """Build the ffmpeg filter_complex for the video transition chain + audio
    mix. Image inputs are 0..n-1; narration audio inputs follow; music (if
    any) is at index `music_in`. `transition` is an ffmpeg `xfade` transition
    name (e.g. "fade", "slideleft", "zoomin") - or "cut" for a hard cut,
    which uses `concat` instead of `xfade` (a 0-duration xfade isn't a
    reliable no-op). `crossfade` must be the SAME value _plan() timed these
    items' overlaps with - see _resolve_animation()/run(). Returns
    (filter_complex, video_label, audio_label|None)."""
    n = len(items)
    parts: list[str] = []

    # --- video: normalize each still, then chain transitions ---
    for i in range(n):
        parts.append(f"[{i}:v]fps={FPS},format=yuv420p,setsar=1[v{i}]")
    if n == 1:
        vlabel = "[v0]"
    elif transition == "cut" or crossfade <= 0:
        vparts = "".join(f"[v{i}]" for i in range(n))
        parts.append(f"{vparts}concat=n={n}:v=1:a=0[vout]")
        vlabel = "[vout]"
    else:
        prev, acc = "[v0]", items[0]["dur"]
        for j in range(1, n):
            off = acc - crossfade
            out = "[vout]" if j == n - 1 else f"[x{j}]"
            parts.append(f"{prev}[v{j}]xfade=transition={transition}:"
                         f"duration={crossfade:.3f}:offset={off:.3f}{out}")
            acc += items[j]["dur"] - crossfade
            prev = out
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
               total: float, out: Path, codec: str,
               transition: str = "fade", crossfade: float = CROSSFADE) -> list[str]:
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stats"]
    # Image inputs (timed, looped to their duration).
    for it in items:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{it['dur']:.3f}",
                "-i", it["still"]]
    # Narration audio inputs, in beat order (only beats with audio).
    for it in items:
        if it["audio"]:
            cmd += ["-i", it["audio"]]
    # Music input (looped) last.
    music_in = None
    if music_path is not None:
        music_in = len(items) + sum(1 for it in items if it["audio"])
        cmd += ["-stream_loop", "-1", "-i", str(music_path)]

    fc, vlabel, alabel = _build_filtergraph(items, music_in, total, transition, crossfade)
    cmd += ["-filter_complex", fc, "-map", vlabel]
    if alabel is not None:
        cmd += ["-map", alabel, "-c:a", "aac", "-b:a", "192k"]

    if codec == "h264_nvenc":
        cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-b:v", "8M"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
    cmd += ["-pix_fmt", "yuv420p", "-r", str(FPS), "-t", f"{total:.3f}", str(out)]
    return cmd


def _assemble_ffmpeg(items: list[dict], music_path: Path | None, total: float, out: Path,
                     transition: str = "fade", crossfade: float = CROSSFADE) -> str:
    """Render with ffmpeg directly. Tries h264_nvenc (if present) then libx264.
    Returns the codec used; raises if every attempt fails."""
    ffmpeg = _ffmpeg()
    codecs = ["h264_nvenc", "libx264"] if _has_nvenc(ffmpeg) else ["libx264"]
    last = ""
    for codec in codecs:
        cmd = _build_cmd(ffmpeg, items, music_path, total, out, codec, transition, crossfade)
        proc = subprocess.run(cmd)
        if proc.returncode == 0:
            return codec
        last = f"ffmpeg exited {proc.returncode} ({codec})"
        print(f"  {codec} failed ({last}); trying next encoder")
    raise RuntimeError(last or "ffmpeg produced no output")


# --------------------------------------------------------------------------- #
# MoviePy fallback (reuses the precomposed stills; only if ffmpeg fails)
# --------------------------------------------------------------------------- #
def _assemble_moviepy(items: list[dict], music_path: Path | None, total: float, out: Path,
                      crossfade: float = CROSSFADE) -> str:
    """MoviePy has no built-in slide/zoom transitions to match ffmpeg's xfade
    styles, so this last-resort fallback (only used if the direct ffmpeg path
    fails outright) only distinguishes "cut" (crossfade<=0 - plain concat, no
    blend) from everything else (a plain crossfade at `crossfade` seconds,
    i.e. today's only look, regardless of slide/zoom being configured)."""
    from moviepy import (ImageClip, AudioFileClip, CompositeAudioClip,
                         concatenate_videoclips)
    from moviepy.video.fx import CrossFadeIn

    clips, to_close = [], []
    for it in items:
        clip = ImageClip(it["still"]).with_duration(it["dur"])
        if it["audio"]:
            narration = (AudioFileClip(it["audio"]).with_start(0.0)
                         .with_volume_scaled(NARRATION_VOL))
            clip = clip.with_audio(CompositeAudioClip([narration]))
        clips.append(clip)
    to_close += clips

    if len(clips) == 1:
        final = clips[0]
    elif crossfade <= 0:
        final = concatenate_videoclips(clips, method="compose")
    else:
        faded = [clips[0]] + [c.with_effects([CrossFadeIn(crossfade)]) for c in clips[1:]]
        final = concatenate_videoclips(faded, method="compose", padding=-crossfade)
    to_close.append(final)

    if music_path is not None and final.audio is not None:
        from moviepy import AudioFileClip as _AFC
        from moviepy.audio.fx import AudioLoop
        track = (_AFC(str(music_path)).with_effects([AudioLoop(duration=final.duration)])
                 .with_volume_scaled(MUSIC_VOL))
        to_close.append(track)
        final = final.with_audio(CompositeAudioClip([final.audio, track]))

    codec = "libx264"
    common = dict(fps=FPS, audio_codec="aac", audio_bitrate="192k",
                  temp_audiofile=str(out.parent / "_recap_temp_audio.m4a"),
                  remove_temp=True, threads=4, logger="bar")
    try:
        final.write_videofile(str(out), codec="h264_nvenc",
                              ffmpeg_params=["-pix_fmt", "yuv420p", "-b:v", "8M"], **common)
        codec = "h264_nvenc"
    except Exception as e:  # noqa: BLE001
        print(f"  h264_nvenc unavailable ({type(e).__name__}); using libx264")
        final.write_videofile(str(out), codec="libx264", preset="medium",
                              ffmpeg_params=["-pix_fmt", "yuv420p"], **common)
    finally:
        for c in to_close:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
    return codec


# --------------------------------------------------------------------------- #
def run(m: Manifest) -> Manifest:
    out_dir = chapter_output_dir(m.chapter_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    beats = m.kept_panels()  # the matched narration beats (STAGE B)
    mapping = _load_mapping(m.chapter_id)  # framer's multi-frame + mode plan
    settings = _load_media_settings(m.chapter_id)  # framer's per-project media settings
    transition, crossfade = _resolve_animation(settings)
    items, total, lines = _plan(beats, mapping, crossfade)
    music_path = _resolve_music(settings)
    wm_cfg = settings.get("watermark")
    print(f"  [debug] media_settings.json watermark cfg: {wm_cfg!r}")
    watermark = _build_watermark_layer(settings)
    print(f"  [debug] watermark: enabled={bool(wm_cfg and wm_cfg.get('enabled'))}, "
          f"layer_built={watermark is not None}, compositing onto {len(items)} still(s)")
    background = _load_background(settings)

    # Per-line report so multi-frame handling can be verified at a glance.
    for ln in lines:
        if ln["mode"] == "sequence" and ln["n_frames"] > 1:
            extra = f" -> {ln['stills']} sub-stills @ ~{ln['frame_dur']:.2f}s each"
        elif ln["mode"] == "together":
            extra = f" -> 1 grid still ({ln['n_frames']} frames at once)"
        else:
            extra = " -> 1 still"
        print(f"  line {ln['order']}: {ln['n_frames']} frame(s), mode={ln['mode']}{extra}")

    (out_dir / "video_plan.json").write_text(
        json.dumps({
            "chapter_id": m.chapter_id, "size": [W, H], "fps": FPS,
            "music": music_path.name if music_path else None,
            "watermark": bool(watermark), "background": bool(background),
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
        _precompose(items, stills_dir, watermark, background)
        anim_desc = "cut (no transition)" if transition == "cut" else f"{transition} @ {crossfade:.2f}s"
        print(f"  precomposed {len(items)} stills ({total:.1f}s timeline), transition={anim_desc}"
              f"{' + watermark' if watermark else ''}{' + custom background' if background else ''}")
        try:
            codec = _assemble_ffmpeg(items, music_path, total, out, transition, crossfade)
            path = "ffmpeg"
        except Exception as e:  # noqa: BLE001 - fall back to MoviePy
            print(f"  ffmpeg assembly failed ({type(e).__name__}: {e}); "
                  f"falling back to MoviePy")
            codec = _assemble_moviepy(items, music_path, total, out, crossfade)
            path = "moviepy"
    finally:
        shutil.rmtree(stills_dir, ignore_errors=True)

    dt = time.perf_counter() - t0
    if music_path:
        print(f"  music bed: {music_path.name} @ vol {MUSIC_VOL} (ducked)")
    print(f"  rendered {len(lines)} lines / {len(items)} stills ({total:.1f}s, "
          f"{FPS}fps, {codec} via {path}) in {dt:.1f}s -> {out}")
    m.stage = "video"
    return m

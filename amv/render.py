"""Beat-synced AMV renderer for vertical Shorts.

Inputs are plain files (ordered frame images, an analysed track, a window); the output is
an mp4. Frames are drawn one by one with OpenCV/numpy and piped to ffmpeg, so every effect
is just a function of the time since the last beat / cut - which is what makes it look edited
by hand. Nothing here knows about chapters or the manifest.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import music

W, H, FPS = 1080, 1920, 30
ZMAX = 1.35                       # headroom baked into every base image
ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / "assets" / "ReCapper.png"
FONTS = [Path("C:/Windows/Fonts/arialbd.ttf"), Path("C:/Windows/Fonts/bahnschrift.ttf")]


# ───────────────────────────── frame loading ─────────────────────────────

def _trim_borders(img: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Cut uniform white/black margins (panel gutters) off all four sides."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    def uniform(line: np.ndarray) -> bool:
        m = float(line.mean())
        return float(line.std()) < 7 and (m > 238 or m < 14)

    top, bot, left, right = 0, h, 0, w
    lim_y, lim_x = int(h * 0.2), int(w * 0.2)
    while top < lim_y and uniform(gray[top]):
        top += 1
    while h - bot < lim_y and bot > top + 8 and uniform(gray[bot - 1]):
        bot -= 1
    while left < lim_x and uniform(gray[:, left]):
        left += 1
    while w - right < lim_x and right > left + 8 and uniform(gray[:, right - 1]):
        right -= 1
    return img[top:bot, left:right], (top, bot, left, right)


def _grade(img: np.ndarray, sat: float = 1.22, contrast: float = 1.08) -> np.ndarray:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[..., 1] = np.clip(hsv[..., 1] * sat, 0, 255)
    out = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR).astype(np.float32)
    out = (out - 127.5) * contrast + 127.5
    return np.clip(out, 0, 255).astype(np.uint8)


def _sharpen(img: np.ndarray, amount: float = 0.5) -> np.ndarray:
    blur = cv2.GaussianBlur(img, (0, 0), 1.4)
    return cv2.addWeighted(img, 1 + amount, blur, -amount, 0)


@dataclass
class Shot:
    path: str
    base: np.ndarray                  # BGR, at least canvas*ZMAX in both axes
    upscale: float                    # how much the source was magnified (>1.6 means soft)
    axis: str                         # 'x' (wide: pan sideways), 'y' (tall: pan up/down) or 'none'
    fit_mode: bool = False
    used: int = 0
    has_face: bool = False
    pivot: tuple[float, float] = (0.5, 0.5)   # eye point, fractions of the trimmed image
    m_face: float = 1.0                       # magnification that gives the face the target size
    tag: dict | None = None


FACE_TARGET_H = 0.30              # a face fills about this share of the screen height
ANCHOR = (0.5, 0.40)              # where the eyes rest on screen, shared by every face shot
MAX_EFFECTIVE_ZOOM = 2.6          # source-pixel magnification beyond which a frame looks soft


POINTS = ("eye", "mouth", "hand", "weapon")
ANCHORS = {"eye": (0.5, 0.40), "mouth": (0.5, 0.46), "hand": (0.5, 0.5), "weapon": (0.5, 0.5)}   # where each point rests on screen


def pivot_of(tag: dict | None, point: str = "eye") -> tuple[float, float, float | None] | None:
    """The tagged focus point as (x, y, size) in fractions of the original frame, or None when the frame does not
    show it. `size` is the face height for eye / mouth (it sets the zoom) and None for a hand or weapon."""
    if not tag:
        return None
    fb = tag.get("face_box") or [0, 0, 0, 0]
    pt = tag.get(point)
    if point in ("eye", "mouth"):
        if not (tag.get("has_face") and fb[3] > fb[1] and pt and pt[0] > 0.01 and pt[1] > 0.01):
            return None
        return float(pt[0]), float(pt[1]), float(fb[3] - fb[1])
    if point in ("hand", "weapon") and pt and pt[0] > 0.01 and pt[1] > 0.01:
        return float(pt[0]), float(pt[1]), None
    return None


def load_shot(path: str | Path, tag: dict | None = None, min_zoom: float = 1.0, point: str | None = None) -> Shot:
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"cannot read {path}")
    h0, w0 = img.shape[:2]
    img, (top, bot, left, right) = _trim_borders(img)
    h, w = img.shape[:2]
    cover = max(W / w, H / h)
    fit = cover > 2.4                 # too small to crop-to-fill: blurred backdrop + sharp panel
    fit_geo = (0, H)                  # fit only: where the panel sits on the canvas (top y, height)
    if fit:
        bg = cv2.resize(img, None, fx=cover, fy=cover, interpolation=cv2.INTER_AREA if cover < 1 else cv2.INTER_CUBIC)
        y0, x0 = (bg.shape[0] - H) // 2, (bg.shape[1] - W) // 2
        bg = cv2.GaussianBlur(bg[y0:y0 + H, x0:x0 + W], (0, 0), 28)
        bg = (bg * 0.55).astype(np.uint8)
        s = W / w
        fg = cv2.resize(img, (W, max(1, int(h * s))), interpolation=cv2.INTER_CUBIC)
        fy = (H - fg.shape[0]) // 2
        fit_geo = (fy, fg.shape[0])
        canvas = bg.copy()
        canvas[fy:fy + fg.shape[0]] = fg[: H - fy] if fy >= 0 else fg[-fy:H - fy]
        scale_total, src = ZMAX, canvas
        base = cv2.resize(src, None, fx=scale_total, fy=scale_total, interpolation=cv2.INTER_CUBIC)
        up, axis = s, "none"
    else:
        scale = cover * ZMAX
        base = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA)
        up = scale
        axis = "x" if base.shape[1] > base.shape[0] * (W / H) * 1.02 else "y" if base.shape[0] > base.shape[1] * (H / W) * 1.02 else "none"
        if up > 1.7:
            base = _sharpen(base, 0.45)
    shot = Shot(str(path), _grade(base), float(up), axis, fit, tag=tag)
    sel = pivot_of(tag, point or "eye")
    if sel and (not fit or point) and h > 0 and w > 0:      # a letterboxed small panel is pinned only on request (a point)
        ex, ey, size = sel
        px = min(0.97, max(0.03, (ex * w0 - left) / w))
        py = min(0.97, max(0.03, (ey * h0 - top) / h))
        share = 1.0                                   # the picture's share of the canvas height
        if fit:                                       # the point's place on the canvas, with the panel centred on it
            share = fit_geo[1] / H
            py = min(0.97, max(0.03, (fit_geo[0] + py * fit_geo[1]) / H))
        cover_eff = W / w if fit else max(W / w, H / h)   # source magnification at m = 1
        m_cap = max(1.0, MAX_EFFECTIVE_ZOOM / cover_eff)
        if size is not None:
            face_h = max(0.05, size * h0 / h * share)
            m = FACE_TARGET_H * H / (face_h * shot.base.shape[0]) * ZMAX
        else:
            m = 1.45                                  # a hand or weapon has no size tag: a moderate close-up
        shot.has_face, shot.pivot = True, (px, py)
        shot.m_face = float(min(max(m, min_zoom), max(m_cap, min_zoom)))
    return shot


# ───────────────────────────── timeline ─────────────────────────────

@dataclass
class Cut:
    t0: float
    t1: float
    kind: str                         # hook | intro | build | drop | post
    downbeat: bool = False
    hero: bool = False
    mode: str = "aura"                # aura | story | burst (amv/styles.py)
    trans: str = "zoom"               # how this cut is entered: zoom (camera push) | whip | dissolve


def _on_hits(a: dict, start: float, length: float, beats: list[float], lo: float, hi: float,
             min_gap: float, floor: float, max_gap: float) -> list[float]:
    """Cut times in [lo, hi) taken from the track's audible hits (strongest first, at least `min_gap` apart),
    with beat-grid times added wherever the hits leave a hole longer than `max_gap`."""
    hits = [(o - start, s) for o, s in a.get("onsets", []) if lo <= o - start < hi and s >= floor]
    chosen: list[float] = []
    for t, _ in sorted(hits, key=lambda x: -x[1]):
        if all(abs(t - c) >= min_gap for c in chosen):
            chosen.append(t)
    chosen.sort()
    out: list[float] = []
    prev = lo
    for t in chosen + [hi]:
        if t - prev > max_gap:
            for b in beats:                                   # fill the hole on the beat grid
                if prev + min_gap <= b <= t - min_gap and b - (out[-1] if out else prev) >= max_gap * 0.6:
                    out.append(b)
        if t < hi:
            out.append(t)
        prev = out[-1] if out else prev
    return sorted(set(out))


def _snap_hit(a: dict, start: float, t: float, reach: float = 0.05, floor: float = 0.3) -> float:
    near = [(s, o - start) for o, s in a.get("onsets", []) if abs(o - start - t) <= reach and s >= floor]
    return max(near)[1] if near else t


def build_cuts(a: dict, start: float, length: float, drop_rel: float) -> list[Cut]:
    """Turn the beat grid into cut times. Dense on the hook, steady in the intro, accelerating
    through the build-up, every beat after the drop, then relaxed."""
    beats = [b - start for b in a["beats"] if start - 0.01 <= b < start + length]
    if len(beats) < 4:
        raise ValueError("window has too few beats")
    period = float(np.median(np.diff(beats)))
    downs = {round(d - start, 3) for d in a["downbeats"]}
    mids = [(x + y) / 2 for x, y in zip(beats[:-1], beats[1:])]
    grid = sorted(set(round(t, 4) for t in beats + mids))

    hook_end = min(1.7, drop_rel * 0.3)
    bar = period * 4
    cuts_t: list[tuple[float, str]] = []
    last = -9.0

    def want(t: float) -> str | None:
        if t >= length - 0.5:
            return None
        if t < hook_end:
            return "hook"
        if t < drop_rel - 2 * bar:
            is_beat = any(abs(t - b) < 0.02 for b in beats)
            idx = min(range(len(beats)), key=lambda i: abs(beats[i] - t))
            return "intro" if is_beat and idx % 2 == 0 else None
        if t < drop_rel - 0.05:
            is_beat = any(abs(t - b) < 0.02 for b in beats)
            if t >= drop_rel - bar:                # last bar: every half beat, the riser
                return "build"
            return "build" if is_beat else None
        if abs(t - drop_rel) < 0.03:
            return "drop"
        if t < drop_rel + 2 * bar:
            is_beat = any(abs(t - b) < 0.02 for b in beats)
            return "drop" if is_beat else None
        if True:
            is_beat = any(abs(t - b) < 0.02 for b in beats)
            idx = min(range(len(beats)), key=lambda i: abs(beats[i] - t))
            return "post" if is_beat and idx % 2 == 0 else None

    cuts_t.append((0.0, "hook"))
    by_hits = bool(a.get("onsets"))
    if by_hits:
        # hook, intro and post follow the audible hits; build and drop keep the beat grid (snapped to a hit)
        intro_end = drop_rel - 2 * bar
        post_start = drop_rel + 2 * bar
        picked = ([(t, "hook") for t in _on_hits(a, start, length, beats, 0.05, hook_end, 0.22, 0.3, 0.6)]
                  + [(t, "intro") for t in _on_hits(a, start, length, beats, hook_end, intro_end, 0.4, 0.45, 1.0)]
                  + [(t, "post") for t in _on_hits(a, start, length, beats, post_start, length - 0.5, 0.5, 0.45, 1.25)])
        for t, kind in picked:
            if kind == "hook" or t - last >= 0.2 or True:
                cuts_t.append((t, kind))
    for t in grid:
        if t <= 0.05:
            continue
        kind = want(t)
        if by_hits and kind in ("hook", "intro", "post"):
            continue
        min_gap = 0.14 if kind == "hook" else 0.2
        if kind and t - last >= min_gap:
            cuts_t.append((_snap_hit(a, start, t) if by_hits and abs(t - drop_rel) > 0.03 else t, kind)); last = t
    cuts_t = sorted(cuts_t)
    spaced = [cuts_t[0]]
    for t, kind in cuts_t[1:]:                       # nothing closer than 0.14s to the cut before it
        if t - spaced[-1][0] >= 0.14:
            spaced.append((t, kind))
    cuts_t = spaced
    # a cut exactly on the drop is the point of the whole edit
    if not any(abs(t - drop_rel) < 0.03 for t, _ in cuts_t):
        cuts_t = [c for c in cuts_t if abs(c[0] - drop_rel) > 0.12] + [(round(drop_rel, 4), "drop")]
        cuts_t.sort()

    cuts: list[Cut] = []
    for i, (t, kind) in enumerate(cuts_t):
        t1 = cuts_t[i + 1][0] if i + 1 < len(cuts_t) else length
        t_s = round(round(t * FPS) / FPS, 4)
        cuts.append(Cut(t_s, round(round(t1 * FPS) / FPS, 4), kind,
                        downbeat=any(abs(t - d) < 0.04 for d in downs),
                        hero=abs(t - drop_rel) < 0.03))
    cuts[-1].t1 = length
    return cuts


def thin_cuts(cuts: list[Cut], n_frames: int, max_hold: float = 2.2) -> list[Cut]:
    """With fewer frames than cuts, merge the shortest neighbouring pair in the relaxed
    sections (post, then intro) - never the hook, build-up or drop - while no shot would
    hold longer than `max_hold`. If that is not enough the pool simply repeats a little."""
    cuts = list(cuts)
    for kind in ("post", "intro"):
        while len(cuts) > n_frames:
            best, best_len = None, max_hold
            for i in range(1, len(cuts)):
                c, prev = cuts[i], cuts[i - 1]
                if c.kind != kind or c.hero or prev.kind != kind:
                    continue
                merged = c.t1 - prev.t0
                if merged < best_len:
                    best, best_len = i, merged
            if best is None:
                break
            cuts[best - 1].t1 = cuts[best].t1
            del cuts[best]
    for a, b in zip(cuts, cuts[1:]):
        a.t1 = b.t0
    return cuts


# ───────────────────────────── branding overlay ─────────────────────────────

def _font(size: int) -> ImageFont.FreeTypeFont:
    for f in FONTS:
        if f.exists():
            return ImageFont.truetype(str(f), size)
    return ImageFont.load_default()


def _text_layer(text: str, size: int) -> Image.Image:
    font = _font(size)
    box = font.getbbox(text)
    layer = Image.new("RGBA", (box[2] - box[0] + 20, size + 30), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((layer.width // 2, layer.height // 2), text, font=font,
                               fill=(255, 255, 255, 255), anchor="mm")
    return layer


# Placement measured from shorts/watermark placement (476x856 screenshot -> 1080x1920):
# name 50% opacity, centred at y=8.3% and y=93.5%; logo 172px at the top-left, fully opaque.
NAME_OPACITY = 0.5
NAME_SIZE = 75
NAME_TOP_CY = 160
NAME_BOTTOM_CY = 1794
LOGO_SIZE, LOGO_XY = 172, (39, 78)


def make_overlay(name: str = "RECAPPER", logo: Path | None = LOGO) -> list[tuple[int, int, np.ndarray, np.ndarray]]:
    """ROI patches (x, y, bgr float32, alpha float32 HxWx1): name top-middle and bottom-middle,
    logo top-left."""
    items: list[tuple[int, int, Image.Image, float]] = []
    t = _text_layer(name.upper(), NAME_SIZE)
    for cy in (NAME_TOP_CY, NAME_BOTTOM_CY):
        items.append(((W - t.width) // 2, cy - t.height // 2, t, NAME_OPACITY))
    if logo and Path(logo).exists():
        lg = Image.open(logo).convert("RGBA")
        lg.thumbnail((LOGO_SIZE, LOGO_SIZE), Image.LANCZOS)
        items.append((LOGO_XY[0], LOGO_XY[1], lg, 1.0))
    out = []
    for x, y, im, opacity in items:
        arr = np.asarray(im, dtype=np.float32)
        out.append((x, y, np.ascontiguousarray(arr[..., [2, 1, 0]]),
                    np.ascontiguousarray(arr[..., 3:4] / 255.0 * opacity)))
    return out


# ───────────────────────────── per-frame drawing ─────────────────────────────

def _ease(u: float) -> float:
    return u * u * (3 - 2 * u)


def _view(shot: Shot, m: float, fx: float, fy: float, dx: float = 0.0, dy: float = 0.0) -> np.ndarray:
    """Window of the base image: magnification m (>=1), centred at fraction (fx, fy)."""
    a = m / ZMAX
    hb, wb = shot.base.shape[:2]
    tx = W / 2 - a * fx * wb + dx
    ty = H / 2 - a * fy * hb + dy
    tx = min(0.0, max(W - a * wb, tx))
    ty = min(0.0, max(H - a * hb, ty))
    M = np.float32([[a, 0, tx], [0, a, ty]])
    return cv2.warpAffine(shot.base, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def _view_pivot(shot: Shot, m: float, anchor: tuple[float, float], dx: float = 0.0, dy: float = 0.0) -> np.ndarray:
    """Magnify by m about the shot's pivot, which stays pinned at the screen anchor (so the eyes
    do not move between shots). Falls back to the nearest legal position near the image edge."""
    a = m / ZMAX
    hb, wb = shot.base.shape[:2]
    px, py = shot.pivot
    tx = anchor[0] * W - a * px * wb + dx
    ty = anchor[1] * H - a * py * hb + dy
    tx = min(0.0, max(W - a * wb, tx))
    ty = min(0.0, max(H - a * hb, ty))
    M = np.float32([[a, 0, tx], [0, a, ty]])
    return cv2.warpAffine(shot.base, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


@dataclass
class Motion:
    kind: str                          # push | pull | pan
    z0: float
    z1: float
    p0: float
    p1: float
    off: float = 0.0


def _motion_for(shot: Shot, cut: Cut, n: int) -> Motion:
    """Deterministic variety: wide frames pan sideways, tall ones slide vertically, the rest
    alternate push/pull. A reused frame starts from a different spot."""
    flip = (n + shot.used) % 2          # only used to vary pan direction / start point
    short = cut.t1 - cut.t0 < 0.45
    z = 0.10 if short else 0.2
    if shot.has_face:
        return Motion("push", 1.0, 1.0 + (0.04 if short else 0.08) * (1.6 if cut.hero else 1.0), 0.5, 0.5)
    if shot.axis in ("x", "y"):
        p0, p1 = (0.1, 0.9) if flip == 0 else (0.9, 0.1)
        return Motion("pan", 1.0, 1.0, p0, p1)           # sliding, never zooming
    return Motion("static", 1.0, 1.0, 0.5, 0.5)


DISSOLVE_S = 0.3
FADE_IN = 0.06                    # short, so a hit on the very first frame is not faded away
WHIP_S = 0.09                     # how long the slide-through of a whip cut lasts on each side of the cut


def _story_view(shot: Shot, mo: Motion, u: float) -> np.ndarray:
    """A slow, quiet move for a story frame: wide/tall frames drift along their long side, the rest push in a little."""
    u = min(1.0, max(0.0, u))
    if mo.kind == "pan":
        p = mo.p0 + (mo.p1 - mo.p0) * u
        fx, fy = (p, 0.5) if shot.axis == "x" else (0.5, p)
        return _view(shot, 1.0, fx, fy)
    return _view(shot, 1.0 + 0.07 * u, 0.5, 0.5)


def _whip(img: np.ndarray, into: bool, out_of: bool, dt_cut: float, dt_end: float, ci: int) -> np.ndarray:
    """Slide + horizontal motion blur on the cut: the old shot is thrown off one side, the new one arrives from the other."""
    sgn = 1 if ci % 2 == 0 else -1
    amt = 0.0
    if into and dt_cut < WHIP_S:
        amt = sgn * (1 - dt_cut / WHIP_S) ** 2                 # arriving: starts far off, settles at 0
    elif out_of and dt_end < WHIP_S:
        amt = -sgn * (1 - dt_end / WHIP_S) ** 2                # leaving: carries on the same way
    if abs(amt) < 0.02:
        return img
    M = np.float32([[1, 0, amt * W * 0.35], [0, 1, 0]])
    img = cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    k = max(3, int(abs(amt) * 150) | 1)
    return cv2.blur(img, (k, 1))


def render(frames: list[str], track: str | Path, out: str | Path, length: float | None = 30.0,
           tempo_scale: float = 1.0, start: float | None = None, drop: float | None = None,
           hero: int = 0, preview: bool = False, name: str = "RECAPPER",
           progress=None, style: str = "push", recipe: str | None = None, items: list[dict] | None = None,
           point: str | None = None) -> Path:
    """`frames`: image paths, or {"path", "tag"} dicts (tag from amv.tagger). `style`: "push"
    (every cut zooms in) or "alt" (cuts alternate zoom-in / zoom-out on the beat). `recipe` (amv.styles:
    aura | story | twist) lays the cuts and picks the frames itself from the bank `items`."""
    a = music.analyze(track, tempo_scale)
    if start is None:
        start, end, drop_rel = music.pick_window(a, length, drop)
        length = end - start                  # auto length ends where the music's section ends
    else:
        drop_rel = (drop if drop is not None else a["drops"][0]) - start
        length = length or 30.0
    length = round(length * FPS) / FPS
    order: list[int] | None = None
    if recipe:
        from . import styles
        plan = styles.build(recipe, items or [], a, start, length, drop_rel, point or "eye")
        cuts, frames, order, hero = plan.cuts, plan.frames, plan.order, plan.hero
    else:
        cuts = thin_cuts(build_cuts(a, start, length, drop_rel), len(frames))
    beats_rel = np.array([b - start for b in a["beats"] if start - 1 <= b <= start + length])
    downs_rel = np.array([d - start for d in a["downbeats"] if start - 1 <= d <= start + length])

    pool = [load_shot(f["path"], f.get("tag"), 1.12 if style == "alt" else 1.0, point) if isinstance(f, dict)
            else load_shot(f, None, 1.12 if style == "alt" else 1.0) for f in frames]
    anchor = ANCHORS.get(point or "eye", ANCHOR)
    dirs = [1 if (style != "alt" or i % 2 == 0) else -1 for i in range(len(cuts) + 1)]
    hero = min(hero, len(pool) - 1)
    if order is None:
        others = [i for i in range(len(pool)) if i != hero] or [hero]
        order = []
        q = 0
        for c in cuts:
            if c.hero:
                order.append(hero)
            else:
                order.append(others[q % len(others)]); q += 1

    sc = 0.5 if preview else 1.0
    ow, oh, fps = int(W * sc), int(H * sc), 15 if preview else FPS
    overlay = make_overlay(name)
    rng = np.random.default_rng(7)

    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    out = Path(out); out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{ow}x{oh}", "-r", str(fps),
           "-i", "-", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(track),
           "-af", f"afade=t=in:d={FADE_IN:g},afade=t=out:st={length - 0.8:.2f}:d=0.8",
           "-c:v", "libx264", "-preset", "veryfast" if preview else "fast", "-crf", "30" if preview else "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-t", f"{length:.3f}",
           "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    # Camera model: every cut is one continuous forward move. The outgoing shot accelerates
    # into the cut, the incoming shot starts at the SAME speed and eases out, and the blur
    # follows the speed - so the eye sees one camera, not a slide followed by a swap.
    def cut_speed(i: int) -> float:
        if i <= 0 or i >= len(cuts):
            return 0.0
        d = min(cuts[i - 1].t1 - cuts[i - 1].t0, cuts[i].t1 - cuts[i].t0)
        return float(np.interp(d, [0.25, 0.5, 1.0, 2.0], [2.6, 1.8, 1.0, 0.6]))

    motions: dict[int, Motion] = {}
    nframes = int(round(length * FPS))
    ci = 0
    try:
        for fi in range(nframes):
            if preview and fi % 2:
                continue                   # preview is 15fps
            t = fi / FPS
            while ci + 1 < len(cuts) and t >= cuts[ci + 1].t0 - 1e-6:
                ci += 1
            cut = cuts[ci]
            shot = pool[order[ci]]
            if ci not in motions:
                motions[ci] = _motion_for(shot, cut, ci)
                shot.used += 1
            mo = motions[ci]
            dur = max(cut.t1 - cut.t0, 1e-3)
            u = (t - cut.t0) / dur
            e = _ease(min(1.0, max(0.0, u)))
            dt_cut = t - cut.t0
            dt_end = max(cut.t1 - t, 0.0)
            prev = beats_rel[beats_rel <= t + 1e-6]
            dt_beat = t - (prev[-1] if len(prev) else -1.0)
            speed = 0.0
            dx = dy = 0.0
            if cut.hero or (cut.kind == "drop" and cut.downbeat):
                amp = (38 if cut.hero else 14) * math.exp(-dt_cut / 0.12)
                dx, dy = float(rng.uniform(-amp, amp)), float(rng.uniform(-amp, amp))

            if cut.mode == "story":
                img = _story_view(shot, mo, u)
                if cut.trans == "dissolve" and ci > 0 and dt_cut < DISSOLVE_S:
                    prev_shot = pool[order[ci - 1]]
                    prev_mo = motions.get(ci - 1) or _motion_for(prev_shot, cuts[ci - 1], ci - 1)
                    k = _ease(dt_cut / DISSOLVE_S)
                    img = cv2.addWeighted(_story_view(prev_shot, prev_mo, 1.0), 1 - k, img, k, 0)
            elif shot.has_face:
                # one continuous camera move through every cut, zooming about the eyes (pinned
                # at the anchor), so the viewer's gaze rests while the picture changes
                m = shot.m_face * (mo.z0 + (mo.z1 - mo.z0) * e)
                tau_out, tau_in = min(0.4, max(0.07, 0.3 * dur)), min(0.45, max(0.08, 0.35 * dur))
                v_out, v_in = cut_speed(ci + 1), cut_speed(ci)
                k_out = v_out * tau_out * math.exp(-dt_end / tau_out)
                m = m * (1 + k_out) if dirs[ci + 1] > 0 else m / (1 + k_out)
                if dirs[ci] > 0:
                    m *= 1 + v_in * tau_in * (1 - math.exp(-dt_cut / tau_in))
                else:
                    m *= 1 + v_in * tau_in * math.exp(-dt_cut / tau_in)
                speed = v_out * math.exp(-dt_end / tau_out) + v_in * math.exp(-dt_cut / tau_in)
                m *= 1 + 0.02 * math.exp(-dt_beat / 0.11)
                if cut.hero:
                    m *= 1 + 0.06 * math.exp(-dt_cut / 0.2)
                m = max(m, 1.0)
                span = 0.035 * speed
                if span > 0.012:
                    steps = 11
                    acc = np.zeros((H, W, 3), np.float32)
                    for k in range(steps):
                        acc += _view_pivot(shot, max(1.0, m * (1 + span * (k / (steps - 1) - 0.5))), anchor, dx, dy)
                    img = (acc / steps).astype(np.uint8)
                else:
                    img = _view_pivot(shot, m, anchor, dx, dy)
            else:
                # no face (battle, scenery, aftermath): never zoom, only slide along the long side
                p = mo.p0 + (mo.p1 - mo.p0) * e
                fx, fy = (p, 0.5) if shot.axis == "x" else (0.5, p) if shot.axis == "y" else (0.5, 0.5)
                img = _view(shot, 1.0, fx, fy, dx, dy)

            nxt = cuts[ci + 1] if ci + 1 < len(cuts) else None
            if cut.trans == "whip" or (nxt is not None and nxt.trans == "whip"):
                img = _whip(img, cut.trans == "whip", nxt is not None and nxt.trans == "whip", dt_cut, dt_end, ci)

            # chromatic split on hits
            if cut.hero or (cut.kind == "drop" and cut.downbeat):
                amt = (16 if cut.hero else 5) * math.exp(-dt_cut / 0.09)
                s = int(round(amt))
                if s >= 1:
                    img = img.copy()
                    img[..., 2] = np.roll(img[..., 2], s, axis=1)
                    img[..., 0] = np.roll(img[..., 0], -s, axis=1)

            # flash
            flash = 0.0
            if cut.hero:
                flash = 0.95 * math.exp(-dt_cut / 0.10)
            elif cut.kind == "drop" and cut.downbeat:
                flash = 0.3 * math.exp(-dt_cut / 0.07)
            if flash > 0.02:
                img = cv2.addWeighted(img, 1 - flash, np.full_like(img, 255), flash, 0)

            # build-up tension: darken then release at the drop
            if cut.kind == "build":
                prog = (t - (drop_rel - 2.5)) / 2.5
                if prog > 0:
                    img = (img * (1 - 0.35 * min(1, prog))).astype(np.uint8)

            # branding
            fimg = img.astype(np.float32)
            for x, y, rgb, al in overlay:
                h2, w2 = rgb.shape[:2]
                roi = fimg[y:y + h2, x:x + w2]
                roi[...] = roi * (1 - al) + rgb * al
            img = np.clip(fimg, 0, 255).astype(np.uint8)

            # fade in/out
            fade = min(1.0, t / FADE_IN) * min(1.0, (length - t) / 0.8)
            if fade < 1.0:
                img = (img * fade).astype(np.uint8)

            if sc != 1.0:
                img = cv2.resize(img, (ow, oh), interpolation=cv2.INTER_AREA)
            proc.stdin.write(img.tobytes())
            if progress and fi % 30 == 0:
                progress(fi / nframes)
        proc.stdin.close()
        proc.wait()
    except BrokenPipeError:
        proc.wait()
        raise RuntimeError("ffmpeg stopped early")
    if proc.returncode:
        raise RuntimeError(f"ffmpeg exited {proc.returncode}")
    return out

"""Builds the shared call-to-action clip (assets/cta/cta.mp4) used by every project's merge.

Add-on, outside the pipeline: it reads the six narration clips in assets/cta/audio/ (the project-name-free
CTA voice), the logo in assets/ReCapper.png and writes one 1080p manga-style animation with synthesized calm
sound effects. No narration text is drawn: the story is told with icons, manga panels, a calendar stamp,
like / subscribe / bell and comment bubbles. The animation is timed from the real length of each voice
clip, so every beat lands on its spoken line.

    python tools/common_cta.py            # full render -> assets/cta/cta.mp4
    python tools/common_cta.py --preview  # 640x360, quicker
    python tools/common_cta.py --still 12.5   # one PNG at that second -> assets/cta/still.png
"""
from __future__ import annotations

import argparse
import math
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageFilter, ImageOps

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ASSETS = ROOT / "assets"
OUT_DIR = ASSETS / "cta"
AUDIO_DIR = OUT_DIR / "audio"
W, H, FPS = 1920, 1080, 30
RED = (230, 57, 70)
INK = (9, 9, 13)
PAPER = (246, 243, 236)
GREY = (40, 40, 48)
SR = 48000

# --------------------------------------------------------------------------- easing
def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))

def prog(t, t0, dur):
    return clamp((t - t0) / dur) if dur > 0 else float(t >= t0)

def out_cubic(x):
    return 1 - (1 - x) ** 3

def in_out(x):
    return 3 * x * x - 2 * x ** 3

def out_back(x, s=1.7):
    x -= 1
    return 1 + (s + 1) * x ** 3 + s * x ** 2

def lerp(a, b, x):
    return a + (b - a) * x

def font(name, size):
    return ImageFont.truetype(str(ASSETS / "fonts" / name), size)

# --------------------------------------------------------------------------- timeline (from voice lengths)
def voice_lengths():
    lens = []
    for k in range(1, 7):
        with open(AUDIO_DIR / f"cta_{k:02d}.wav", "rb") as f:
            pass
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(AUDIO_DIR / f"cta_{k:02d}.wav")], capture_output=True, text=True, check=True)
        lens.append(float(out.stdout.strip()))
    return lens

LEAD, GAP, TAIL = 0.9, 0.55, 1.6
INTRO_LOGO = 3.1        # the logo shows this long, then a second frame takes the rest of line 1

def timeline(lens):
    starts, t = [], LEAD
    for d in lens:
        starts.append(t)
        t += d + GAP
    return starts, t - GAP + TAIL

# --------------------------------------------------------------------------- sprites (drawn big, shrunk smooth)
SS = 3

def canvas(size):
    return Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))

def finish(img, size):
    return img.resize((size, size), Image.LANCZOS)

def icon_book(size=400):
    im = canvas(size); d = ImageDraw.Draw(im); s = size * SS
    o = s * 0.035
    L = [(s*.08, s*.28), (s*.48, s*.36), (s*.48, s*.86), (s*.08, s*.78)]
    R = [(s*.52, s*.36), (s*.92, s*.28), (s*.92, s*.78), (s*.52, s*.86)]
    for poly in (L, R):
        d.polygon(poly, fill=PAPER, outline=INK, width=int(o))
    for i in range(5):
        y = .46 + i * .07
        d.line([(s*.14, s*y - s*.02), (s*.42, s*(y + .04))], fill=INK, width=int(s*.018))
        d.line([(s*.58, s*(y + .04)), (s*.86, s*y - s*.02)], fill=INK, width=int(s*.018))
    d.polygon([(s*.46, s*.34), (s*.54, s*.34), (s*.54, s*.92), (s*.46, s*.88)], fill=RED)
    return finish(im, size)

def icon_pen(size=400):
    im = canvas(size); d = ImageDraw.Draw(im); s = size * SS
    body = [(s*.62, s*.08), (s*.88, s*.34), (s*.34, s*.80), (s*.16, s*.84), (s*.20, s*.66)]
    d.polygon(body, fill=PAPER, outline=INK, width=int(s*.035))
    d.polygon([(s*.62, s*.08), (s*.88, s*.34), (s*.78, s*.44), (s*.52, s*.18)], fill=RED, outline=INK, width=int(s*.03))
    d.polygon([(s*.20, s*.66), (s*.34, s*.80), (s*.16, s*.84)], fill=INK)
    d.line([(s*.12, s*.92), (s*.50, s*.92)], fill=PAPER, width=int(s*.03))
    return finish(im, size)

def icon_mic(size=400):
    im = canvas(size); d = ImageDraw.Draw(im); s = size * SS
    d.rounded_rectangle([s*.34, s*.08, s*.66, s*.56], radius=s*.16, fill=PAPER, outline=INK, width=int(s*.035))
    for i in range(4):
        y = s * (.18 + i * .09)
        d.line([(s*.38, y), (s*.62, y)], fill=INK, width=int(s*.02))
    d.arc([s*.22, s*.28, s*.78, s*.76], 20, 160, fill=PAPER, width=int(s*.05))
    d.line([(s*.5, s*.76), (s*.5, s*.9)], fill=PAPER, width=int(s*.05))
    d.line([(s*.34, s*.92), (s*.66, s*.92)], fill=RED, width=int(s*.06))
    return finish(im, size)

def icon_clap(size=400):
    im = canvas(size); d = ImageDraw.Draw(im); s = size * SS
    d.rectangle([s*.1, s*.38, s*.9, s*.88], fill=INK, outline=PAPER, width=int(s*.035))
    top = [(s*.08, s*.18), (s*.88, s*.06), (s*.92, s*.30), (s*.12, s*.40)]
    d.polygon(top, fill=PAPER, outline=INK, width=int(s*.03))
    for i in range(5):
        x = s * (.14 + i * .15)
        d.polygon([(x, s*.38), (x + s*.07, s*.37), (x + s*.17, s*.09), (x + s*.10, s*.10)], fill=INK)
    d.polygon([(s*.4, s*.5), (s*.4, s*.76), (s*.64, s*.63)], fill=RED)
    return finish(im, size)

def icon_moon(size=400):
    im = canvas(size); s = size * SS
    m = Image.new("L", im.size, 0); md = ImageDraw.Draw(m)
    md.ellipse([s*.12, s*.12, s*.88, s*.88], fill=255)
    md.ellipse([s*.34, s*.04, s*1.0, s*.70], fill=0)
    im.paste(Image.new("RGBA", im.size, PAPER + (255,)), (0, 0), m)
    d = ImageDraw.Draw(im)
    for cx, cy, r in ((.72, .30, .035), (.84, .52, .025), (.62, .18, .02)):
        d.ellipse([s*(cx-r), s*(cy-r), s*(cx+r), s*(cy+r)], fill=PAPER)
    return finish(im, size)

def icon_thumb(size=420, fill=PAPER):
    im = canvas(size); d = ImageDraw.Draw(im); s = size * SS
    w = int(s * .035)
    d.rounded_rectangle([s*.08, s*.44, s*.28, s*.90], radius=s*.04, fill=RED, outline=INK, width=w)
    hand = [(s*.30, s*.48), (s*.44, s*.44), (s*.56, s*.16), (s*.64, s*.10), (s*.72, s*.16), (s*.70, s*.38),
            (s*.90, s*.40), (s*.94, s*.52), (s*.90, s*.60), (s*.92, s*.70), (s*.86, s*.78), (s*.86, s*.86),
            (s*.78, s*.92), (s*.30, s*.90)]
    d.polygon(hand, fill=fill, outline=INK, width=w)
    for y in (.60, .70, .80):
        d.line([(s*.50, s*y), (s*.84, s*y)], fill=INK, width=int(s*.015))
    return finish(im, size)

def icon_bell(size=400):
    im = canvas(size); d = ImageDraw.Draw(im); s = size * SS
    w = int(s * .035)
    d.pieslice([s*.18, s*.12, s*.82, s*.80], 180, 360, fill=PAPER, outline=INK, width=w)
    d.polygon([(s*.18, s*.46), (s*.82, s*.46), (s*.92, s*.78), (s*.08, s*.78)], fill=PAPER, outline=INK, width=w)
    d.rectangle([s*.18, s*.44, s*.82, s*.5], fill=PAPER)
    d.ellipse([s*.40, s*.76, s*.60, s*.94], fill=RED, outline=INK, width=w)
    d.ellipse([s*.44, s*.04, s*.56, s*.16], fill=PAPER, outline=INK, width=w)
    return finish(im, size)

def icon_heart(size=300, color=RED):
    im = canvas(size); d = ImageDraw.Draw(im); s = size * SS
    pts = []
    for i in range(0, 361, 4):
        a = math.radians(i)
        x = 16 * math.sin(a) ** 3
        y = 13 * math.cos(a) - 5 * math.cos(2*a) - 2 * math.cos(3*a) - math.cos(4*a)
        pts.append((s * (.5 + x * .029), s * (.46 - y * .029)))
    d.polygon(pts, fill=color, outline=INK, width=int(s * .035))
    return finish(im, size)

def icon_cursor(size=200):
    im = canvas(size); d = ImageDraw.Draw(im); s = size * SS
    d.polygon([(s*.2, s*.08), (s*.2, s*.84), (s*.4, s*.66), (s*.54, s*.94), (s*.68, s*.88), (s*.54, s*.60), (s*.80, s*.58)],
              fill=PAPER, outline=INK, width=int(s * .045))
    return finish(im, size)

def bubble(kind, w=420, h=300, tail="left", fill=PAPER):
    im = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    s = SS; bw = int(8 * s)
    box = [bw, bw, w * s - bw, (h - 60) * s]
    d.ellipse(box, fill=fill, outline=INK, width=bw)
    tx = w * s * (.28 if tail == "left" else .72)
    d.polygon([(tx - 30*s, (h - 90) * s), (tx + (-50 if tail == "left" else 50) * s, (h - 4) * s), (tx + 30*s, (h - 100) * s)],
              fill=fill, outline=INK, width=bw)
    d.ellipse([bw + 6, bw + 6, w * s - bw - 6, (h - 60) * s - 6], outline=None, fill=fill)
    cx, cy = w * s / 2, (h - 60) * s / 2
    if kind == "?":
        d.text((cx, cy), "?", font=font("Anton-Regular.ttf", 190 * s // 1), fill=RED, anchor="mm")
    elif kind == "!":
        d.text((cx, cy), "!", font=font("Anton-Regular.ttf", 190 * s // 1), fill=RED, anchor="mm")
    elif kind == "...":
        for i in (-1, 0, 1):
            d.ellipse([cx + i * 70 * s - 22 * s, cy - 22 * s, cx + i * 70 * s + 22 * s, cy + 22 * s], fill=INK)
    elif kind == "heart":
        hs = icon_heart(150).resize((150 * s, 150 * s), Image.LANCZOS)
        im.alpha_composite(hs, (int(cx - 75 * s), int(cy - 70 * s)))
    elif kind == "star":
        pts = []
        for i in range(10):
            r = 78 * s if i % 2 == 0 else 34 * s
            a = math.radians(-90 + i * 36)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        d.polygon(pts, fill=RED, outline=INK, width=int(5 * s))
    return im.resize((w, h), Image.LANCZOS)

def icon_pause(size=360):
    im = canvas(size); d = ImageDraw.Draw(im); s = size * SS
    d.ellipse([s*.06, s*.06, s*.94, s*.94], fill=INK, outline=RED, width=int(s*.05))
    for x in (.34, .56):
        d.rounded_rectangle([s*x, s*.28, s*(x + .1), s*.72], radius=s*.02, fill=PAPER)
    return finish(im, size)

CHAR_DIR = OUT_DIR / "new characters"      # chibi stickers: ch1.. (transparent PNGs, named ch<N>-...)
SPRITE_H = 300                              # every character is normalised to this height; mascots scale from it

def character_sprite(path, height=SPRITE_H, border=6):
    """A transparent chibi PNG trimmed, scaled to `height`, with a white sticker outline."""
    im = Image.open(path).convert("RGBA")
    box = im.getchannel("A").point(lambda v: 255 if v > 20 else 0).getbbox()
    if box:
        im = im.crop(box)
    r = height / im.height
    im = im.resize((max(1, int(im.width * r)), height), Image.LANCZOS)
    pad = border + 4
    canvas_ = Image.new("RGBA", (im.width + 2 * pad, im.height + 2 * pad), (0, 0, 0, 0))
    canvas_.alpha_composite(im, (pad, pad))
    a_ = canvas_.getchannel("A").point(lambda v: 255 if v > 40 else 0)
    grown = a_.filter(ImageFilter.MaxFilter(border * 2 + 1)).filter(ImageFilter.GaussianBlur(0.8))
    out = Image.new("RGBA", canvas_.size, PAPER + (0,))
    out.putalpha(grown)
    out.alpha_composite(canvas_)
    return out

def load_characters():
    """{number: sprite} of the chibi stickers in assets/cta/new characters/ (ch1-..., ch2-...)."""
    out = {}
    for f in sorted(CHAR_DIR.glob("ch*.png")):
        digits = "".join(ch for ch in f.stem.split("-")[0] if ch.isdigit())
        if digits:
            out[int(digits)] = character_sprite(f)
    return out

def subscribe_button(done=False, w=640, h=190):
    im = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0)); d = ImageDraw.Draw(im); s = SS
    col = (58, 58, 66) if done else RED
    d.rounded_rectangle([6*s, 6*s, (w - 6) * s, (h - 6) * s], radius=h * s // 2 - 6*s, fill=col, outline=PAPER, width=7*s)
    label = "SUBSCRIBED" if done else "SUBSCRIBE"
    d.text((w * s / 2 + (40 * s if not done else 0), h * s / 2), label, font=font("Poppins-ExtraBold.ttf", 62 * s), fill=PAPER, anchor="mm")
    if not done:
        d.polygon([(70*s, 55*s), (70*s, 135*s), (140*s, 95*s)], fill=PAPER)
        d.text((w * s / 2 + 40 * s, h * s / 2), label, font=font("Poppins-ExtraBold.ttf", 62 * s), fill=PAPER, anchor="mm")
    return im.resize((w, h), Image.LANCZOS)

# --------------------------------------------------------------------------- compositing helpers
class Frame:
    def __init__(self, base: Image.Image):
        self.im = base.convert("RGBA")

    def put(self, sprite, cx, cy, scale=1.0, rot=0.0, alpha=1.0, glow=None):
        if alpha <= 0.003 or scale <= 0.003:
            return
        sp = sprite
        if scale != 1.0:
            sp = sp.resize((max(1, int(sp.width * scale)), max(1, int(sp.height * scale))), Image.BILINEAR)
        if rot:
            sp = sp.rotate(rot, resample=Image.BICUBIC, expand=True)
        if alpha < 1.0:
            a = sp.getchannel("A").point(lambda v: int(v * alpha))
            sp = sp.copy(); sp.putalpha(a)
        if glow:
            g = Image.new("RGBA", sp.size, glow + (0,)); g.putalpha(sp.getchannel("A").filter(ImageFilter.GaussianBlur(26)).point(lambda v: min(255, int(v * 0.9))))
            pad = Image.new("RGBA", (sp.width + 0, sp.height + 0), (0, 0, 0, 0))
            self.im.alpha_composite(g, (int(cx - sp.width / 2), int(cy - sp.height / 2)))
        self.im.alpha_composite(sp, (int(cx - sp.width / 2), int(cy - sp.height / 2))) if (
            0 <= cx - sp.width / 2 and 0 <= cy - sp.height / 2 and cx + sp.width / 2 <= W and cy + sp.height / 2 <= H) else self._clipped(sp, cx, cy)

    def _clipped(self, sp, cx, cy):
        x0, y0 = int(cx - sp.width / 2), int(cy - sp.height / 2)
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        layer.paste(sp, (x0, y0))
        self.im.alpha_composite(layer)

    def draw(self):
        return ImageDraw.Draw(self.im)

def make_halftone():
    yy, xx = np.mgrid[0:H + 64, 0:W + 64].astype(np.float32)
    u = (xx + yy) * 0.7071; v = (xx - yy) * 0.7071
    pitch = 22.0
    fu = (u % pitch) - pitch / 2; fv = (v % pitch) - pitch / 2
    dist = np.sqrt(fu * fu + fv * fv)
    grad = np.clip(1.0 - np.abs(yy - H * .5) / (H * .62), 0, 1) ** 1.2        # bigger dots toward the middle band
    radius = 1.0 + 6.0 * grad * (0.35 + 0.65 * np.clip((xx / W), 0, 1) ** 0.5)
    mask = np.clip((radius - dist) * 1.6, 0, 1)
    return mask  # float32 (H+64, W+64)

HALF = (make_halftone() * 255).astype(np.uint8)   # uint8: light on memory
_BASE = None

def base_page():
    """The ink-black page with its vignette, made once."""
    global _BASE
    if _BASE is None:
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        d = np.sqrt(((xx - W / 2) / (W * .62)) ** 2 + ((yy - H / 2) / (H * .72)) ** 2)
        v = np.clip(1.15 - d * .55, 0.35, 1.0)
        arr = (np.array(INK, np.float32)[None, None, :] * v[..., None]).astype(np.uint8)
        _BASE = Image.fromarray(arr)
    return _BASE

def background(t, mood=0.0, tint=1.0):
    """Ink-black page with drifting red/grey halftone. `mood` 0 calm .. 1 strong dots; `tint` fades the dots out."""
    ox, oy = int(t * 14) % 64, int(t * 8) % 64
    k = (0.35 + 0.65 * mood) * tint
    m = Image.fromarray(HALF[oy:oy + H, ox:ox + W]).point(lambda v: int(v * k * 0.42))
    im = base_page().copy()
    im.paste((140, 36, 46), mask=m)
    return im

def speed_lines(fr: Frame, t, strength, cx=W / 2, cy=H / 2, color=PAPER, inner=330):
    if strength <= 0.01:
        return
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
    rng = np.random.default_rng(int(t * 12) % 1000)
    n = 90
    for i in range(n):
        a = 2 * math.pi * i / n + rng.uniform(-.02, .02)
        wd = rng.uniform(.004, .014)
        r0 = inner + rng.uniform(0, 260)
        r1 = r0 + rng.uniform(380, 1300) * strength
        pts = [(cx + r0 * math.cos(a - wd), cy + r0 * math.sin(a - wd)), (cx + r1 * math.cos(a), cy + r1 * math.sin(a)),
               (cx + r0 * math.cos(a + wd), cy + r0 * math.sin(a + wd))]
        d.polygon(pts, fill=color + (int(150 * strength),))
    fr.im.alpha_composite(layer)

def flash(fr: Frame, amount, color=PAPER):
    if amount > 0.01:
        fr.im.alpha_composite(Image.new("RGBA", (W, H), color + (int(255 * clamp(amount)),)))

def panel_frame(w, h, skew=28, fill=INK, border=PAPER, bw=9):
    """A manga panel: a slanted quad with a paper border and a red inner line."""
    im = Image.new("RGBA", (w * SS + 2 * SS * skew, h * SS), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    s = SS
    quad = [(skew * s, 0), (im.width, 0), (im.width - skew * s, h * s), (0, h * s)]
    d.polygon(quad, fill=border)
    ins = bw * s
    q2 = [(skew * s + ins * 1.6, ins), (im.width - ins * .4, ins), (im.width - skew * s - ins * 1.6, h * s - ins), (ins * .4, h * s - ins)]
    d.polygon(q2, fill=fill)
    return im.resize((im.width // SS, im.height // SS), Image.LANCZOS)

# --------------------------------------------------------------------------- the Marvel-style comic wall
PANEL_DIR = OUT_DIR / "panels"
WALL_COLS = 5

def build_wall():
    """WALL_COLS tall strips of black-and-white manga panels (from the series' own chapters), alternating up/down."""
    import random
    files = sorted(PANEL_DIR.glob("*.jpg"))
    if not files:
        return None
    rng = random.Random(4)
    cw = W // WALL_COLS + 6
    strips = []
    for c in range(WALL_COLS):
        pick = files[:]
        rng.shuffle(pick)
        pieces, h = [], 0
        for f in pick[:14]:
            im = Image.open(f).convert("L")
            im = im.resize((cw - 10, max(200, int(im.height * (cw - 10) / im.width))), Image.LANCZOS)
            pieces.append(im); h += im.height + 12
        strip = Image.new("L", (cw, h), 0)
        y = 6
        for im in pieces:
            strip.paste(im, (5, y)); y += im.height + 12
        strips.append(strip)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    d = np.sqrt(((xx - W / 2) / (W * .5)) ** 2 + ((yy - H / 2) / (H * .5)) ** 2)
    scrim = Image.fromarray(np.clip(255 * (0.34 + 0.66 * np.clip(d - 0.1, 0, 1) ** 0.8), 0, 255).astype(np.uint8))
    return {"strips": strips, "cw": cw, "scrim": scrim}

_WALL = None

def comic_wall(t, dim, speed_k=1.0):
    """The page-flipping wall: columns scroll fast in opposite directions. `dim` 1..0 is its brightness."""
    global _WALL
    if _WALL is None:
        _WALL = build_wall() or {}
    if not _WALL:
        return background(t, 0.4)
    cw, strips = _WALL["cw"], _WALL["strips"]
    page = Image.new("L", (W, H), 0)
    # distance travelled: very fast at first, easing as the logo lands
    dist = 2600 * (1 - math.exp(-t * 1.6)) * 0.55 + t * 260 * speed_k
    for c, strip in enumerate(strips):
        sign = 1 if c % 2 == 0 else -1
        off = int((dist * (0.85 + 0.12 * c)) * sign) % strip.height
        part = Image.new("L", (cw, H), 0)
        top = strip.crop((0, off, cw, min(strip.height, off + H)))
        part.paste(top, (0, 0))
        if top.height < H:
            part.paste(strip.crop((0, 0, cw, H - top.height)), (0, top.height))
        page.paste(part, (c * (W // WALL_COLS) - 3, 0))
        # motion smear: a faint copy a few pixels along the scroll direction
    smear = ImageChops.offset(page, 0, 14).point(lambda v: int(v * 0.45))
    page = ImageChops.lighter(page, smear)
    page = ImageChops.multiply(page, _WALL["scrim"])
    flick = 0.9 + 0.1 * math.sin(t * 61)                   # projector flicker
    page = page.point(lambda v: min(255, int(v * dim * flick * 1.7)))
    rgb = ImageOps.colorize(page, black=(6, 6, 9), white=(238, 234, 226))
    return rgb

# --------------------------------------------------------------------------- the sprite set
class Art:
    def __init__(self):
        logo = Image.open(ASSETS / "ReCapper.png").convert("RGBA")
        self.logo = logo.resize((720, 720), Image.LANCZOS)
        self.logo_small = logo.resize((200, 200), Image.LANCZOS)
        self.book, self.pen, self.mic, self.clap = icon_book(), icon_pen(), icon_mic(), icon_clap()
        self.moon = icon_moon(360)
        self.thumb = icon_thumb(430)
        self.bell = icon_bell(300)
        self.heart = icon_heart(220)
        self.cursor = icon_cursor(150)
        self.sub = subscribe_button(False)
        self.sub_done = subscribe_button(True)
        self.panel = panel_frame(330, 440)
        self.bub = {k: bubble(k) for k in ("?", "!", "...", "heart", "star")}
        self.bub_r = {k: bubble(k, tail="right") for k in ("?", "!", "...", "heart", "star")}
        self.wordmark = ImageFont.truetype(str(ASSETS / "fonts" / "Anton-Regular.ttf"), 120)
        self.pause = icon_pause()
        self.chars = load_characters()

# --------------------------------------------------------------------------- scenes
def scene_logo(fr, art, t, T, outro=False, delay=0.0):
    """T = (start, duration) of the scene. Logo with a red aura, ring pulse and the wordmark."""
    s0, dur = T
    lt = t - s0 - delay
    if lt < 0:
        return
    a_in = out_cubic(prog(lt, 0, 0.9))
    sc = lerp(1.18, 1.0, out_cubic(prog(lt, 0, 1.2))) * (1 + 0.012 * math.sin(lt * 2.2))
    cy = H / 2 - 40 + 8 * math.sin(lt * 1.6)
    # pulse rings
    for k in range(3):
        ph = ((lt * 0.55 - k / 3) % 1.0)
        r = 380 + 520 * ph
        al = (1 - ph) * 0.55 * a_in
        ring = Image.new("RGBA", (W, H), (0, 0, 0, 0)); ImageDraw.Draw(ring).ellipse([W/2 - r, cy - r, W/2 + r, cy + r], outline=RED + (int(255 * al),), width=6)
        fr.im.alpha_composite(ring)
    fr.put(art.logo, W / 2, cy, sc, alpha=a_in, glow=RED)
    if outro:
        mascot(fr, art, 2, "l", lt, 0.6, y=H - 560, scale=0.8, flip=True)       # presents the logo
        mascot(fr, art, 8, "r", lt, 0.9, y=H - 560, scale=0.8)                   # waves
        mascot(fr, art, 13, "l", lt, 1.3, y=H - 190, scale=0.55)
        mascot(fr, art, 6, "r", lt, 1.6, y=H - 250, scale=0.62)                   # applause
    # wordmark, letters drop in one by one
    word = "RECAPPER"
    d = fr.draw()
    total = d.textlength(word, font=art.wordmark) + 14 * (len(word) - 1)
    x = W / 2 - total / 2
    for i, ch in enumerate(word):
        p = out_back(prog(lt, 0.9 + i * 0.07, 0.45))
        w_ = d.textlength(ch, font=art.wordmark)
        a = prog(lt, 0.9 + i * 0.07, 0.25)
        if a > 0:
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0)); ld = ImageDraw.Draw(layer)
            y = H - 175 - (1 - p) * 60
            ld.text((x + 4, y + 5), ch, font=art.wordmark, fill=RED + (int(255 * a),))
            ld.text((x, y), ch, font=art.wordmark, fill=PAPER + (int(255 * a),))
            fr.im.alpha_composite(layer)
        x += w_ + 14

def scene_makers(fr, art, t, T):
    """Line 2: four panels slam in - read, script, voice, edit - under a moon (late nights)."""
    s0, dur = T
    lt = t - s0
    mp = out_cubic(prog(lt, 0, 1.0))
    fr.put(art.moon, W - 250, 210 - (1 - mp) * 90, 0.85 + 0.03 * math.sin(lt), alpha=mp * 0.95, glow=RED)
    mascot(fr, art, 1, "l", lt, 0.5, y=H - 150, scale=0.62, hold=dur - 0.6)       # the washing boy: the hard work
    mascot(fr, art, 12, "r", lt, 1.8, y=H - 175, scale=0.62, hold=dur - 0.6)      # hand on chin: scripting
    icons = [art.book, art.pen, art.mic, art.clap]
    xs = [W / 2 + (i - 1.5) * 400 for i in range(4)]
    out = in_out(prog(lt, dur - 0.55, 0.5))
    for i, ic in enumerate(icons):
        t0 = 0.35 + i * 0.95
        p = prog(lt, t0, 0.45)
        if p <= 0:
            continue
        sc = lerp(1.7, 1.0, out_cubic(p)) if p < 1 else 1.0
        sc *= 1 + 0.018 * math.sin(lt * 3 + i)
        bob = 6 * math.sin(lt * 1.8 + i)
        cy = H / 2 + 20 + bob + out * 420
        fr.put(art.panel, xs[i], cy, sc, rot=(-3 if i % 2 else 3) * (1 - 0.4 * out_cubic(p)), alpha=min(1, p * 2))
        fr.put(ic, xs[i] + 12, cy, sc * 0.72, alpha=min(1, p * 2.4))
        shock = clamp(1 - (lt - t0 - 0.28) / 0.25) if lt > t0 + 0.28 else 0
        if shock > 0:
            speed_lines(fr, lt * 3, shock * .5, xs[i], cy, color=RED, inner=170)

def scene_like_sub(fr, art, t, T):
    """Line 4: thumbs up bursts, a cursor presses SUBSCRIBE, the button turns grey and the bell rings."""
    s0, dur = T
    lt = t - s0
    # thumb (left)
    tp = out_back(prog(lt, 0.25, 0.55))
    liked = lt > 1.55
    bump = 1 + (0.22 * math.sin(clamp((lt - 1.55) / 0.35) * math.pi) if liked else 0)
    tx, ty = W / 2 - 430, H / 2 - 30
    if liked:
        speed_lines(fr, lt * 2, clamp(1 - (lt - 1.55) / .6) * .9, tx, ty, color=RED, inner=210)
    thumb = icon_thumb(430, fill=(255, 205, 205)) if liked else art.thumb
    fr.put(thumb, tx, ty, tp * bump, rot=-8 * (1 - clamp((lt - .25) / .7)), alpha=min(1, tp * 2), glow=RED if liked else None)
    # hearts that float out of the like
    if liked:
        for k in range(5):
            hp = prog(lt, 1.6 + k * 0.12, 1.3)
            if hp > 0:
                fr.put(art.heart, tx + (k - 2) * 70 + 40 * math.sin(hp * 5 + k), ty - 140 - 300 * out_cubic(hp), 0.3 + 0.1 * k % .3,
                       alpha=1 - hp)
    # subscribe button (right)
    sp = out_back(prog(lt, 2.05, 0.5))
    bx, by = W / 2 + 330, H / 2 + 120
    clicked = lt > 3.35
    press = 1 - 0.1 * math.sin(clamp((lt - 3.3) / 0.25) * math.pi)
    btn = art.sub_done if clicked else art.sub
    fr.put(btn, bx, by, sp * press, alpha=min(1, sp * 2))
    # cursor travels in and presses
    cp = in_out(prog(lt, 2.4, 0.9))
    cx = lerp(W + 100, bx + 120, cp); cy = lerp(H + 100, by + 40, cp) + 6 * math.sin(lt * 9) * (1 - cp)
    if lt > 2.4:
        fr.put(art.cursor, cx, cy, 1.0 - 0.1 * (1 if 3.3 < lt < 3.5 else 0), alpha=1 - prog(lt, 3.9, .4))
    mascot(fr, art, 4, "l", lt, 0.7, y=H - 205, scale=0.82, hold=dur - 0.4)           # thumbs up
    mascot(fr, art, 11, "r", lt, 2.2, y=H - 215, scale=0.78, hold=dur - 0.3)          # points at the button
    # bell
    if clicked:
        bp = out_back(prog(lt, 3.5, 0.5))
        sw = math.sin((lt - 3.5) * 16) * 22 * math.exp(-(lt - 3.5) * 1.1)
        fr.put(art.bell, bx - 360 + 0, by - 190, bp, rot=sw, alpha=min(1, bp * 2), glow=RED)
        ringp = prog(lt, 3.55, .7)
        if ringp < 1:
            for sgn in (-1, 1):
                for k in range(2):
                    r = 20 + k * 22 + 70 * ringp
                    arc = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                    ImageDraw.Draw(arc).arc([bx - 360 - 150 - r, by - 190 - 130 - r, bx - 360 + 150 + r, by - 190 + 130 + r],
                                            (-30 if sgn > 0 else 150), (30 if sgn > 0 else 210), fill=RED + (int(255 * (1 - ringp)),), width=10)
                    fr.im.alpha_composite(arc)

def scene_comments(fr, art, t, T):
    """Line 5: comment bubbles pop up one after another: a big question (which story next?) and reactions."""
    s0, dur = T
    lt = t - s0
    layout = [  # kind, x, y, scale, side, t0
        ("...", W / 2 - 620, H / 2 - 110, 0.95, "l", 0.35),
        ("heart", W / 2 + 560, H / 2 - 200, 0.9, "r", 0.85),
        ("star", W / 2 - 430, H / 2 + 230, 0.85, "l", 1.35),
        ("!", W / 2 + 470, H / 2 + 190, 0.95, "r", 1.85),
        ("?", W / 2, H / 2 - 20, 1.65, "l", 2.35),
    ]
    mascot(fr, art, 12, "l", lt, 0.2, y=H - 190, scale=0.85, hold=dur - 0.3)          # thinking: which story next?
    mascot(fr, art, 5, "r", lt, 1.0, y=H - 190, scale=0.85, hold=dur - 0.3)           # OK sign
    for kind, x, y, sc, side, t0 in layout:
        p = out_back(prog(lt, t0, 0.5), 2.0)
        if p <= 0:
            continue
        spr = (art.bub if side == "l" else art.bub_r)[kind]
        fl = 10 * math.sin(lt * 1.7 + x)
        extra = (1 + 0.05 * math.sin(lt * 4)) if kind == "?" and lt > t0 + .6 else 1
        fr.put(spr, x, y + fl, sc * p * extra, alpha=min(1, p * 2.4), glow=RED if kind == "?" else None)
        if 0 < lt - t0 < 0.3:
            speed_lines(fr, lt * 3, (1 - (lt - t0) / .3) * .45, x, y, color=RED, inner=170 * sc)


def mascot(fr, art, key, side, lt, t0, y=None, scale=1.0, hold=None, dur_out=0.4, flip=False):
    """Character `key` (a sticker number) peeks in from the `side` edge at `t0`, bobs, and leaves at `hold`."""
    sp = art.chars.get(key)
    if sp is None:
        return
    if flip:
        sp = sp.transpose(Image.FLIP_LEFT_RIGHT)
    p = out_back(prog(lt, t0, 0.5))
    away = in_out(prog(lt, hold, dur_out)) if hold is not None else 0.0
    vis = p * (1 - away)
    if vis <= 0.01:
        return
    w = sp.width * scale
    x = (w * 0.40 - (1 - vis) * w) if side == "l" else (W - w * 0.40 + (1 - vis) * w)
    yy = (y if y is not None else H - 170) + 10 * math.sin(lt * 3 + key)
    fr.put(sp, x, yy, scale, rot=(-7 if side == "l" else 7) * (1 - 0.3 * vis) + 3 * math.sin(lt * 2 + key), alpha=1.0)

def scene_intro2(fr, art, t, T):
    """Second frame of line 1: the PAUSE button turns into PLAY, and a strip of manga panels with the series'
    characters slides through - 'a lot goes into bringing these stories to life'."""
    s0, dur = T
    lt = t - s0
    pp = out_back(prog(lt, 0.05, 0.45))
    flip = in_out(prog(lt, 0.95, 0.45))
    icon = art.pause
    if flip > 0.5:
        icon = Image.new("RGBA", art.pause.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(icon); w_, h_ = icon.size
        d.ellipse([w_ * .06, h_ * .06, w_ * .94, h_ * .94], fill=RED, outline=PAPER, width=int(w_ * .03))
        d.polygon([(w_ * .40, h_ * .28), (w_ * .40, h_ * .72), (w_ * .72, h_ * .5)], fill=PAPER)
    squash = abs(math.cos(flip * math.pi)) if flip < 1 else 1.0
    fr.put(icon, W / 2, 230, 0.9 * pp * (0.35 + 0.65 * squash) ** 0.5, alpha=min(1, pp * 2), glow=RED)
    keys = (3, 10, 14)       # Luffy, Gojo, Goku: three loud faces of three different stories
    for i in range(3):
        t0 = 1.0 + i * 0.28
        p = out_cubic(prog(lt, t0, 0.7))
        if p <= 0:
            continue
        x = W / 2 + (i - 1) * 470 + (1 - p) * 900
        y = H / 2 + 150 + 10 * math.sin(lt * 1.7 + i) + (1 if i % 2 else -1) * 18
        fr.put(art.panel, x, y, 0.95, rot=(-4, 2, -2)[i], alpha=min(1, p * 2))
        sp = art.chars.get(keys[i])
        if sp is not None:
            pop_ = out_back(prog(lt, t0 + 0.35, 0.45))
            fr.put(sp, x + 8, y + 40 - 10 * pop_, 0.95 * pop_, rot=(-4, 2, -2)[i], alpha=min(1, pop_ * 2))
        else:
            fr.put([art.book, art.mic, art.clap][i], x + 8, y, 0.7, alpha=min(1, p * 2))
    sh = clamp(1 - (lt - 1.2) / 0.4) if lt > 1.2 else 0
    if sh > 0:
        speed_lines(fr, lt * 3, sh * .6, W / 2, 230, color=RED, inner=170)

CAL_COLS, CAL_ROWS, CAL_DAYS, CAL_START = 7, 5, 30, 2     # 30 days, the 1st on a Wednesday

def daily_times(dur):
    """When each calendar day gets its character: slow at first, then faster and faster."""
    times, tt, gap = [], 0.75, 0.30
    for _ in range(CAL_DAYS):
        times.append(tt)
        tt += gap
        gap = max(0.07, gap * 0.87)
    return times

def scene_daily(fr, art, t, T):
    """Line 3: a wall calendar. Each day gets a chibi character stamped on it, faster and faster - a new chapter every day."""
    s0, dur = T
    lt = t - s0
    cw, rh = 212, 124
    cal_w, head, wk = CAL_COLS * cw, 112, 56
    cal_h = head + wk + CAL_ROWS * rh
    x0, y0 = W / 2 - cal_w / 2 + 40, H / 2 - cal_h / 2 + 24
    a_in = out_cubic(prog(lt, 0, 0.6))
    out = in_out(prog(lt, dur - 0.4, 0.4))
    dy = (1 - a_in) * 120 + out * 60
    al = a_in * (1 - out)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
    a8 = int(255 * al)
    d.rounded_rectangle([x0 + 14, y0 + dy + 16, x0 + cal_w + 14, y0 + dy + cal_h + 16], radius=26, fill=(0, 0, 0, int(140 * al)))
    d.rounded_rectangle([x0, y0 + dy, x0 + cal_w, y0 + dy + cal_h], radius=26, fill=PAPER + (a8,), outline=INK + (a8,), width=6)
    d.rounded_rectangle([x0, y0 + dy, x0 + cal_w, y0 + dy + head], radius=26, fill=RED + (a8,), outline=INK + (a8,), width=6)
    d.rectangle([x0 + 3, y0 + dy + head - 30, x0 + cal_w - 3, y0 + dy + head - 3], fill=RED + (a8,))
    for k in range(6):
        rx = x0 + cal_w * (k + .5) / 6
        d.rounded_rectangle([rx - 11, y0 + dy - 26, rx + 11, y0 + dy + 38], radius=11, fill=(30, 30, 38, a8), outline=PAPER + (a8,), width=4)
    f_day = font("Anton-Regular.ttf", 38)
    for c, letter in enumerate("MTWTFSS"):
        d.text((x0 + cw * (c + .5), y0 + dy + head + wk / 2), letter, font=f_day, fill=(RED if c >= 5 else INK) + (a8,), anchor="mm")
    for k in range(5):
        sx = x0 + cal_w / 2 + (k - 2) * 90
        pts = []
        for i in range(10):
            r = 24 if i % 2 == 0 else 10
            a = math.radians(-90 + i * 36)
            pts.append((sx + r * math.cos(a), y0 + dy + head / 2 + 4 + r * math.sin(a)))
        d.polygon(pts, fill=PAPER + (a8,))
    gy = y0 + dy + head + wk
    for r in range(CAL_ROWS + 1):
        d.line([(x0 + 6, gy + r * rh), (x0 + cal_w - 6, gy + r * rh)], fill=(150, 146, 140, a8), width=3)
    for c in range(1, CAL_COLS):
        d.line([(x0 + c * cw, gy), (x0 + c * cw, gy + CAL_ROWS * rh)], fill=(150, 146, 140, a8), width=3)
    f_num = font("Poppins-Bold.ttf", 28)
    for i in range(CAL_DAYS):
        r, c = divmod(i + CAL_START, CAL_COLS)
        d.text((x0 + c * cw + 12, gy + r * rh + 8), str(i + 1), font=f_num, fill=(80, 78, 84, a8))
    fr.im.alpha_composite(layer)
    times = daily_times(dur)
    rng = np.random.default_rng(11)
    pool = [k for k in sorted(art.chars) if k != 6]          # 6 is the big realistic clapper: kept for the finale
    order = []
    for i in range(CAL_DAYS):                   # random, but never the same one twice in a row
        k = pool[int(rng.integers(0, len(pool)))] if pool else 0
        while len(pool) > 1 and order and k == order[-1]:
            k = pool[int(rng.integers(0, len(pool)))]
        order.append(k)
    for i in range(CAL_DAYS):
        p = prog(lt, times[i], 0.26)
        if p <= 0:
            continue
        r, c = divmod(i + CAL_START, CAL_COLS)
        cx, cy = x0 + c * cw + cw / 2 + 12, gy + r * rh + rh / 2 + 10 + dy
        ring_p = prog(lt, times[i], 0.35)
        if ring_p < 1:
            rg = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            rr = 34 + 70 * ring_p
            ImageDraw.Draw(rg).ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=RED + (int(255 * (1 - ring_p)),), width=7)
            fr.im.alpha_composite(rg)
        if art.chars:
            sp = art.chars[order[i]]
            fit = min(112 / sp.height, 178 / sp.width)      # inside the day cell
            sc = fit * lerp(2.0, 1.0, out_cubic(p))
            fr.put(sp, cx, cy + 4, sc, rot=-6 + (i % 5) * 3, alpha=min(1, p * 2.4) * al)
        else:
            stamp = Image.new("RGBA", (120, 120), (0, 0, 0, 0)); sd = ImageDraw.Draw(stamp)
            sd.ellipse([6, 6, 114, 114], outline=RED, width=10)
            sd.line([(30, 62), (52, 86), (92, 38)], fill=RED, width=14)
            fr.put(stamp, cx, cy, lerp(2.0, 1.0, out_cubic(p)), alpha=min(1, p * 2.2) * al)
    last = times[-1] + 0.12
    if lt > last:
        speed_lines(fr, lt * 2, clamp(1 - (lt - last) / .5) * .8, color=RED)
    mascot(fr, art, 3, "l", lt, 1.4, y=H - 200, scale=0.8, hold=dur - 0.5)       # Luffy cheers it on
    mascot(fr, art, 14, "r", lt, 2.4, y=H - 200, scale=0.8, hold=dur - 0.5, flip=True)   # Goku

SCENES = {}

def build_scenes(starts, lens, total):
    ends = [s + d for s, d in zip(starts, lens)]
    return {
        "logo1": (0.0, INTRO_LOGO),
        "intro2": (INTRO_LOGO, starts[1] - INTRO_LOGO),
        "makers": (starts[1], starts[2] - starts[1]),
        "daily": (starts[2], starts[3] - starts[2]),
        "likesub": (starts[3], starts[4] - starts[3]),
        "comments": (starts[4], starts[5] - starts[4]),
        "logo2": (starts[5], total - starts[5]),
    }

def render_frame(art, sc, t, total):
    mood = {"logo1": .3, "intro2": .5, "makers": .6, "daily": .7, "likesub": .9, "comments": .6, "logo2": .35}
    name = max((k for k in sc if sc[k][0] <= t + 1e-6), key=lambda k: sc[k][0], default="logo1")
    s0, dur = sc[name]
    prev_name = None
    if name in ("logo1", "intro2"):
        lt0 = t - s0 if name == "logo1" else (t - sc["logo1"][0])      # wall time runs on across both frames
        dim = 0.95 if t < 0.65 else lerp(0.95, 0.42, out_cubic(prog(t, 0.65, 0.5)))
        if name == "intro2":
            dim = lerp(0.42, 0.30, prog(t, sc["intro2"][0], 0.6))
            fade = in_out(prog(t, sc["makers"][0] - 0.45, 0.45))
        else:
            fade = 0.0
        wall = comic_wall(t, dim, 1.0)
        if fade > 0:
            wall = Image.blend(wall, background(t, mood[name]), fade)
        fr = Frame(wall)
    else:
        fr = Frame(background(t, mood[name]))
    # cross-fade the previous scene's elements with an ink-wipe: draw scenes with a short overlap
    fns = {"intro2": scene_intro2, "makers": scene_makers, "daily": scene_daily, "likesub": scene_like_sub, "comments": scene_comments}
    lt = t - s0
    if name in ("logo1", "logo2"):
        scene_logo(fr, art, t, (s0, dur), outro=(name == "logo2"), delay=0.7 if name == "logo1" else 0.0)
    else:
        fns[name](fr, art, t, (s0, dur))
        # small logo bug top-left stays through the middle scenes
        fr.put(art.logo_small, 130, 130, 1.0, alpha=out_cubic(prog(lt, 0, .5)) * .95)
    # scene-change ink flash
    flash(fr, clamp(1 - lt / 0.22) * .55 if name not in ("logo1",) else 0)
    # intro: fade up from black over the first 0.5s; outro: fade to black at the end
    fade_in = prog(t, 0, .5)
    fade_out = prog(t, total - 0.9, 0.9)
    k = fade_in * (1 - fade_out)
    if k < 1:
        fr.im.alpha_composite(Image.new("RGBA", (W, H), (0, 0, 0, int(255 * (1 - k)))))
    return fr.im.convert("RGB")

# --------------------------------------------------------------------------- audio
def read_voice(path):
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(out, np.float32)

def env(n, a=0.005, r=0.2, curve=2.0):
    x = np.ones(n, np.float32)
    na, nr = int(a * SR), int(r * SR)
    if na: x[:na] = np.linspace(0, 1, na)
    if nr and nr < n: x[-nr:] *= np.linspace(1, 0, nr) ** curve
    return x

def whoosh(dur=0.9, lo=300, hi=2400, up=True):
    from scipy.signal import butter, sosfilt
    n = int(dur * SR); noise = np.random.default_rng(3).standard_normal(n).astype(np.float32)
    out = np.zeros(n, np.float32); step = n // 18
    for i in range(18):
        f = lerp(lo, hi, i / 17) if up else lerp(hi, lo, i / 17)
        sos = butter(2, [f * .7, min(f * 1.5, SR / 2 - 100)], btype="band", fs=SR, output="sos")
        out[i * step:(i + 1) * step if i < 17 else n] = sosfilt(sos, noise[i * step:(i + 1) * step if i < 17 else n])
    x = np.sin(np.linspace(0, np.pi, n)) ** 2
    return out * x * 0.35

def thump(dur=0.5, f0=95, f1=42):
    n = int(dur * SR); tt = np.arange(n) / SR
    f = f1 + (f0 - f1) * np.exp(-tt * 18)
    ph = 2 * np.pi * np.cumsum(f) / SR
    return (np.sin(ph) * np.exp(-tt * 7)).astype(np.float32) * 0.6

def tick(dur=0.05):
    n = int(dur * SR); noise = np.random.default_rng(5).standard_normal(n).astype(np.float32)
    return noise * np.exp(-np.arange(n) / SR * 90) * 0.18

def chime(freq=880, dur=1.4, amp=0.28):
    n = int(dur * SR); tt = np.arange(n) / SR
    x = sum(a * np.sin(2 * np.pi * freq * m * tt) * np.exp(-tt * d) for m, a, d in ((1, 1, 3.0), (2.0, .35, 4.5), (3.01, .15, 6)))
    return (x * amp * env(n, .004, 0.05)).astype(np.float32)

def bell_ring(dur=2.0):
    n = int(dur * SR); tt = np.arange(n) / SR
    x = sum(a * np.sin(2 * np.pi * f * tt) * np.exp(-tt * d) for f, a, d in ((1318, 1, 2.2), (2490, .5, 3.0), (3520, .3, 4.2), (660, .35, 1.6)))
    return (x * 0.22 * env(n, .002, .1)).astype(np.float32)

def pop(f=520, dur=0.18):
    n = int(dur * SR); tt = np.arange(n) / SR
    fr_ = f * (1 + 1.2 * np.exp(-tt * 40))
    return (np.sin(2 * np.pi * np.cumsum(fr_) / SR) * np.exp(-tt * 22)).astype(np.float32) * 0.3

def pad(total):
    n = int(total * SR); tt = np.arange(n) / SR
    x = np.zeros(n, np.float32)
    for f, a in ((110, .5), (164.8, .35), (220, .3), (329.6, .15)):
        lfo = 0.8 + 0.2 * np.sin(2 * np.pi * (0.07 + f / 6000) * tt)
        x += (np.sin(2 * np.pi * f * tt) * a * lfo).astype(np.float32)
    x *= np.minimum(1, tt / 2.0) * np.minimum(1, (total - tt) / 2.0)
    return x * 0.07

def mix_audio(starts, lens, total, sc, out_wav):
    n = int(total * SR) + SR
    track = np.zeros(n, np.float32); sfx = np.zeros(n, np.float32)
    def add(buf, x, t0, gain=1.0):
        i = int(t0 * SR)
        if i < 0: return
        x = x[: max(0, len(buf) - i)]
        buf[i:i + len(x)] += x * gain
    for k, (s, d) in enumerate(zip(starts, lens), start=1):
        v = read_voice(AUDIO_DIR / f"cta_{k:02d}.wav")
        add(track, v, s)
    # --- sfx, placed from the same timings the animation uses
    add(sfx, whoosh(0.7, 200, 2600), 0.0, 0.8)
    for i in range(16):                                   # page-flip riffle, slowing as the logo lands
        add(sfx, tick(.05), 0.02 + i * (0.035 + i * 0.0032), 1.3 - i * 0.03)
    add(sfx, thump(.7, 150, 38), 0.68, 1.0); add(sfx, chime(660, 2.0, .32), 0.75); add(sfx, chime(990, 1.6, .15), 0.95)
    for i in range(8): add(sfx, pop(700 + i * 40, .12), 1.6 + i * 0.07, 0.35)
    s0 = sc["intro2"][0]
    add(sfx, whoosh(.7, 250, 1500), s0 - .05, .5); add(sfx, pop(520), s0 + .08, 1.0)
    add(sfx, tick(.04), s0 + .95, 1.4); add(sfx, chime(784, 1.4, .22), s0 + .98)
    for i in range(3):
        add(sfx, whoosh(.55, 300, 1300), s0 + 1.0 + i * .28, .4); add(sfx, thump(.35), s0 + 1.35 + i * .28, .6)
    s0 = sc["makers"][0]
    add(sfx, whoosh(.8, 200, 1500), s0 - .1, .6)
    for i in range(4):
        t0 = s0 + 0.35 + i * 0.95 + .1
        add(sfx, thump(), t0, 0.9); add(sfx, tick(), t0, 1.0); add(sfx, chime([523, 587, 659, 784][i], 1.0, .14), t0 + .05)
    s0 = sc["daily"][0]
    add(sfx, whoosh(.8, 300, 2000), s0 - .1, .6)
    times = daily_times(sc["daily"][1])
    for i, tt in enumerate(times):
        add(sfx, thump(.3, 120, 55), s0 + tt + .05, .5 if i < 18 else .32); add(sfx, tick(), s0 + tt + .05, .8)
        if i % 5 == 4: add(sfx, chime([523, 587, 659, 784, 880, 988][i // 5 % 6], .9, .1), s0 + tt + .05)
    add(sfx, chime(1046, 2.0, .2), s0 + times[-1] + .12)
    s0 = sc["likesub"][0]
    add(sfx, whoosh(.6, 300, 1400), s0 + .1, .5); add(sfx, pop(480), s0 + .3, 1.0)
    add(sfx, chime(784, 1.4, .26), s0 + 1.55); add(sfx, chime(1175, 1.2, .16), s0 + 1.6); add(sfx, thump(.4), s0 + 1.55, .8)
    add(sfx, whoosh(.5, 400, 1600), s0 + 2.05, .5); add(sfx, whoosh(.9, 200, 1200, up=False), s0 + 2.4, .35)
    add(sfx, tick(.04), s0 + 3.33, 1.6); add(sfx, pop(400, .2), s0 + 3.35, .9)
    add(sfx, bell_ring(2.2), s0 + 3.5)
    s0 = sc["comments"][0]
    for i, t0 in enumerate((.35, .85, 1.35, 1.85, 2.35)):
        add(sfx, pop(420 + i * 90), s0 + t0, 1.0); add(sfx, chime([659, 784, 880, 988, 1175][i], .9, .12), s0 + t0 + .04)
    s0 = sc["logo2"][0]
    add(sfx, whoosh(1.0, 250, 1700), s0 - .05, .6); add(sfx, chime(660, 2.6, .3), s0 + .2); add(sfx, chime(990, 2.2, .16), s0 + .4)
    add(sfx, chime(1320, 2.0, .1), s0 + .6)
    p = pad(total)
    # duck sfx/pad under the voice
    vox = np.abs(track[:len(sfx)])
    win = int(0.12 * SR)
    smooth = np.convolve(vox, np.ones(win, np.float32) / win, mode="same")
    duck = 1 - 0.55 * np.clip(smooth * 12, 0, 1)
    mixed = track + sfx * duck + np.pad(p, (0, n - len(p)))[:n] * duck
    peak = np.max(np.abs(mixed)) or 1
    mixed = mixed / peak * 0.89
    mixed = mixed[: int(total * SR)]
    pcm = (np.clip(mixed, -1, 1) * 32767).astype("<i2")
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())

# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--still", type=float)
    ap.add_argument("--out", default=str(OUT_DIR / "cta.mp4"))
    a = ap.parse_args()
    lens = voice_lengths()
    starts, total = timeline(lens)
    sc = build_scenes(starts, lens, total)
    art = Art()
    print(f"voice {sum(lens):.1f}s, clip {total:.1f}s, scenes at " + ", ".join(f"{k}@{v[0]:.1f}" for k, v in sc.items()), flush=True)
    if a.still is not None:
        render_frame(art, sc, a.still, total).save(OUT_DIR / "still.png")
        print("wrote", OUT_DIR / "still.png"); return
    wav = OUT_DIR / "_mix.wav"
    mix_audio(starts, lens, total, sc, wav)
    out = Path(a.out)
    size = (640, 360) if a.preview else (W, H)
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(wav), "-vf", f"scale={size[0]}:{size[1]}:flags=lanczos,format=yuv420p",
           "-c:v", "libx264", "-preset", "medium" if not a.preview else "veryfast", "-crf", "17", "-profile:v", "high",
           "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "1", "-shortest", "-movflags", "+faststart", str(out)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    nframes = int(total * FPS)
    for i in range(nframes):
        p.stdin.write(render_frame(art, sc, i / FPS, total).tobytes())
        if i % 60 == 0:
            print(f"  frame {i}/{nframes}", flush=True)
    p.stdin.close(); p.wait()
    wav.unlink(missing_ok=True)
    import json
    out.with_suffix(".json").write_text(json.dumps({"duration_s": round(total, 1), "size": [W, H], "shared": True}), encoding="utf-8")
    print("wrote", out)

if __name__ == "__main__":
    main()

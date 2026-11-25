"""On-screen words for the quote and lyrics shorts: a serif face, words that rise out of a blur as they are spoken,
accent colour on the words that carry the idea, and a line that leaves the same way (lifting and blurring away).

`TextLayer(pages, accent=...)` is built from `Page`s (lists of timed words); `draw(img, t)` returns the frame with the
words composited (and the picture dimmed under them so they read). Words are drawn once to small patches; per frame only
the patches that are visible are blurred (while they move) and blended. No file is written here.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "assets" / "fonts"
# name -> (file, weight, accent weight); a weight of None means a static font (one weight)
FONTS: dict[str, tuple[str, int | None, int | None]] = {
    "montserrat": ("Montserrat[wght].ttf", 800, 900),
    "oswald": ("Oswald[wght].ttf", 700, 700),
    "anton": ("Anton-Regular.ttf", None, None),
    "poppins": ("Poppins-ExtraBold.ttf", None, None),
    "archivo": ("ArchivoBlack-Regular.ttf", None, None),
    "barlow": ("BarlowCondensed-ExtraBold.ttf", None, None),
    "bebas": ("BebasNeue-Regular.ttf", None, None),
    "cormorant": ("CormorantGaramond[wght].ttf", 600, 700),      # the serif used before
    "cinzel": ("Cinzel[wght].ttf", 700, 800),                    # classical capitals
    "playfair": ("PlayfairDisplay[wght].ttf", 800, 900),
}
DEFAULT_FONT = "anton"
FALLBACKS = [Path("C:/Windows/Fonts/arialbd.ttf"), Path("C:/Windows/Fonts/segoeuib.ttf")]
W, H = 1080, 1920
MAX_WIDTH = 800            # the block stays on the left; the speaker's frame has the right side
MARGIN_X = 72
CENTER_Y = 0.46            # vertical centre of the block (fraction of the height)
IN_S, OUT_S = 0.42, 0.40   # word entrance / line exit durations
BASE = (232, 236, 240)     # BGR: warm off-white
WEIGHT, WEIGHT_ACCENT = 600, 700


@dataclass
class TWord:
    text: str
    t0: float
    t1: float
    accent: bool = False


@dataclass
class Page:
    words: list[TWord]
    t_in: float            # the first word starts here
    t_out: float           # the page starts to leave here


_FONT_CACHE: dict[tuple, ImageFont.FreeTypeFont] = {}


def _font(size: int, accent: bool = False, name: str = DEFAULT_FONT) -> ImageFont.FreeTypeFont:
    key = (size, accent, name)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    file, weight, weight_accent = FONTS.get(name, FONTS[DEFAULT_FONT])
    w = weight_accent if accent else weight
    for path in [FONT_DIR / file] + FALLBACKS:
        if path.exists():
            f = ImageFont.truetype(str(path), size)
            if w is not None and path.name == file:
                try:
                    f.set_variation_by_axes([w])
                except (OSError, AttributeError):
                    pass
            _FONT_CACHE[key] = f
            return f
    return ImageFont.load_default()


def _ease(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return 1 - (1 - u) ** 3


def _smooth(u: float) -> float:
    u = min(1.0, max(0.0, u))
    return u * u * (3 - 2 * u)


@dataclass
class _Patch:
    shadow: np.ndarray      # float32 alpha 0..1
    mask: np.ndarray        # float32 alpha 0..1
    x: int
    y: int
    line: int
    word: TWord
    color: tuple[int, int, int]


class TextLayer:
    def __init__(self, pages: list[Page], accent: tuple[int, int, int] = (90, 170, 255), dim: float = 0.42,
                 center_y: float = CENTER_Y, font: str = DEFAULT_FONT, align: str = "left",
                 max_width: int = MAX_WIDTH, size: int = 104, margin_x: int = MARGIN_X):
        self.max_width, self.size0, self.margin_x = max_width, size, margin_x
        self.font = font if font in FONTS else DEFAULT_FONT
        self.align = align                  # "left" (block on the left margin) or "center"
        self.pages = pages
        self.accent = accent
        self.dim = dim
        self.center_y = center_y
        self.patches: list[list[_Patch]] = [self._layout(p) for p in pages]

    # ── layout ──
    def _layout(self, page: Page) -> list[_Patch]:
        size = self.size0
        while True:
            f, fb = _font(size, False, self.font), _font(size, True, self.font)
            space = f.getlength(" ")
            lines: list[list[tuple[TWord, float]]] = [[]]
            width = 0.0
            for w in page.words:
                wl = (fb if w.accent else f).getlength(w.text)
                if lines[-1] and width + space + wl > self.max_width:
                    lines.append([]); width = 0.0
                width += (space if lines[-1] else 0) + wl
                lines[-1].append((w, wl))
            if len(lines) <= 7 or size <= 56:
                break
            size -= 4
        lh = size * 1.28
        top = H * self.center_y - lh * len(lines) / 2
        out: list[_Patch] = []
        for li, line in enumerate(lines):
            total = sum(wl for _, wl in line) + space * (len(line) - 1)
            x = self.margin_x if self.align == "left" else (W - total) / 2
            for w, wl in line:
                font = fb if w.accent else f
                pad = 18
                bb = font.getbbox(w.text)
                pw, ph = int(wl) + pad * 2, int(size * 1.5) + pad
                img = Image.new("L", (pw, ph), 0)
                ImageDraw.Draw(img).text((pad, pad // 2), w.text, font=font, fill=255)
                mask = np.asarray(img, np.float32) / 255
                shadow = cv2.GaussianBlur(mask, (0, 0), 7) * 1.4
                out.append(_Patch(np.clip(shadow, 0, 1), mask, int(x) - pad, int(top + li * lh) - pad // 2, li, w,
                                  self.accent if w.accent else BASE))
                x += wl + space
        return out

    # ── drawing ──
    def _page_state(self, i: int, t: float) -> float:
        """0 before the page starts, 1 while it is up, rising/falling around that (for the dim)."""
        p = self.pages[i]
        return _smooth((t - (p.t_in - 0.5)) / 0.5) * (1 - _smooth((t - p.t_out) / (OUT_S + 0.15)))

    def draw(self, img: np.ndarray, t: float) -> np.ndarray:
        active = [i for i, p in enumerate(self.pages) if p.t_in - 0.5 <= t <= p.t_out + OUT_S + 0.5]
        if not active:
            return img
        # the picture dims under the words so they can be read
        d = max(self._page_state(i, t) for i in active) * self.dim
        if d > 0.01:
            band = np.linspace(0, 1, H, dtype=np.float32)
            weight = 0.55 + 0.45 * np.exp(-((band - self.center_y) / 0.28) ** 2)
            img = (img.astype(np.float32) * (1 - d * weight).reshape(H, 1, 1)).astype(np.uint8)
        for i in active:
            page = self.pages[i]
            leave = t - page.t_out
            for p in self.patches[i]:
                a_in = _ease((t - p.word.t0) / IN_S)
                if a_in <= 0:
                    continue
                a, dy, sigma = a_in, (1 - a_in) * 24.0, (1 - a_in) * 7.0
                if leave > 0:
                    k = _ease((leave - 0.06 * p.line) / OUT_S)
                    a *= 1 - k
                    dy -= k * 30.0
                    sigma = max(sigma, k * 8.0)
                if a <= 0.01:
                    continue
                self._blend(img, p, a, dy, sigma)
        return img

    @staticmethod
    def _blend(img: np.ndarray, p: _Patch, a: float, dy: float, sigma: float) -> None:
        mask, shadow = p.mask, p.shadow
        if sigma > 0.6:
            mask = cv2.GaussianBlur(mask, (0, 0), sigma)
            shadow = cv2.GaussianBlur(shadow, (0, 0), sigma)
        h, w = mask.shape
        x0, y0 = p.x, int(round(p.y + dy))
        xa, ya = max(0, x0), max(0, y0)
        xb, yb = min(img.shape[1], x0 + w), min(img.shape[0], y0 + h)
        if xb <= xa or yb <= ya:
            return
        sl = (slice(ya - y0, yb - y0), slice(xa - x0, xb - x0))
        roi = img[ya:yb, xa:xb].astype(np.float32)
        sh = (shadow[sl] * a * 0.7)[..., None]
        m = (mask[sl] * a)[..., None]
        roi = roi * (1 - sh)
        roi = roi * (1 - m) + np.array(p.color, np.float32) * m
        img[ya:yb, xa:xb] = np.clip(roi, 0, 255).astype(np.uint8)


def pages_from_words(words: list[tuple[str, float, float, int]], accents: set[str], offset: float,
                     per_page: int = 15, hold: float = 0.12, by_sentence: bool = False) -> list[Page]:
    """`words` = (text, t0, t1, sentence) in narration time; `offset` moves them to clip time. Sentences are grouped
    into pages of at most `per_page` words (a long sentence is cut at a comma when it has one)."""
    pages: list[list[tuple[str, float, float, int]]] = []
    cur: list[tuple[str, float, float, int]] = []
    last_sentence = None
    for w in words:
        if cur and ((w[3] != last_sentence and (by_sentence or len(cur) + 1 > per_page * 0.6)) or len(cur) >= per_page):
            pages.append(cur); cur = []
        cur.append(w)
        last_sentence = w[3]
    if cur:
        pages.append(cur)
    out = []
    for grp in pages:
        tw = [TWord(t, a + offset, b + offset, re.sub(r"\W", "", t).lower() in accents) for t, a, b, _ in grp]
        out.append(Page(tw, tw[0].t0, tw[-1].t1 + hold))
    return out

"""Cinematic words for the quote short: type that lives inside the picture instead of lying on top of it.

`CinemaText` is a `textlayer.TextLayer` with the look of a title in a film. What it looks like (font, where it sits, how the
words arrive, the rule beside the block) comes from an `amv.looks.Look`; what every look shares:

  * a soft, local darkening under the block (the art stays bright elsewhere), not a flat dim;
  * a band of light that sweeps through the letters as a word arrives;
  * the words that carry the idea are lit in the accent colour: a glow that breathes with the narrator's voice and a
    thin line that draws itself under the word as it is spoken;
  * the block drifts and grows a little with the camera push (it sits in front of the picture, so it moves more than
    the picture does), so it never sits still on the plate;
  * the line leaves as it came: lifting, blurring and burning out to light.

Entrances: `rise` (out of a blur, from below), `slam` (drops in large with a flash, hard and quick) and `track` (slides in
sideways and fades up, slow).

`voice_envelope(wav, start)` reads the narration file once and gives `env(t)` in 0..1 (clip seconds): how loud the voice is
right now. It drives the glow here and the lights of the backdrop (`amv.backdrop`), so the picture answers the voice.
Nothing here writes a file.
"""
from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
import soundfile as sf

from .looks import Look, get as get_look
from .textlayer import H, OUT_S, TextLayer, W, _ease, _smooth

WHITE = np.array([255.0, 255.0, 255.0], np.float32)
ENTRANCE_S = {"rise": 0.5, "slam": 0.17, "track": 0.75}


def voice_envelope(wav: str | Path, start: float, fps: int = 60):
    """env(t): the narration's loudness (0..1, smoothed, clip seconds) from the wav that starts at `start`."""
    try:
        data, rate = sf.read(str(wav), dtype="float32", always_2d=True)
    except Exception:  # noqa: BLE001 - no readable narration: the lights simply do not follow a voice
        return lambda t: 0.0
    x = data.mean(axis=1)
    hop = max(1, rate // fps)
    n = len(x) // hop
    if n < 2:
        return lambda t: 0.0
    rms = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1))
    k = max(1, int(fps * 0.08))
    rms = np.convolve(rms, np.ones(k) / k, mode="same")
    top = float(np.percentile(rms, 95)) or 1.0
    env = np.clip(rms / top, 0, 1) ** 0.8

    def at(t: float) -> float:
        i = (t - start) * fps
        if i < 0 or i >= n - 1:
            return 0.0
        j = int(i)
        return float(env[j] + (env[j + 1] - env[j]) * (i - j))
    return at


class Stack:
    """Several text layers drawn one after the other (the quote, then the closing message)."""

    def __init__(self, *layers):
        self.layers = layers

    def draw(self, img: np.ndarray, t: float) -> np.ndarray:
        for layer in self.layers:
            img = layer.draw(img, t)
        return img


class CinemaText(TextLayer):
    def __init__(self, pages, accent=(90, 170, 255), font: str | None = None, hold_end: float = 10.0, env=None,
                 look: Look | str | None = None):
        self.look = look if isinstance(look, Look) else get_look(look)
        lk = self.look
        if lk.upper:
            for page in pages:
                for w in page.words:
                    w.text = w.text.upper()
        super().__init__(pages, accent=accent, dim=0.0, center_y=lk.center_y, font=font or lk.font, align=lk.align,
                         max_width=lk.max_width, size=lk.size, margin_x=lk.margin_x)
        self.hold_end = max(1.0, hold_end)
        self.env = env or (lambda t: 0.0)
        self.extra: list[list[tuple[np.ndarray, int]]] = []
        self.bounds: list[tuple[int, int, int, int]] = []         # per page: left, top, right, bottom of the block
        for patches in self.patches:
            row = []
            for p in patches:
                glow = np.clip(cv2.GaussianBlur(p.mask, (0, 0), 15) * 2.2, 0, 1)
                ys = np.where(p.mask.max(axis=1) > 0.5)[0]
                row.append((glow, int(ys.max()) + 9 if len(ys) else p.mask.shape[0] - 6))
            self.extra.append(row)
            if patches:
                self.bounds.append((min(p.x + 18 for p in patches), min(p.y + 18 for p in patches),
                                    max(p.x + p.mask.shape[1] - 18 for p in patches), max(p.y + p.mask.shape[0] - 18 for p in patches)))
            else:
                self.bounds.append((0, 0, 0, 0))

    # ── the picture under the words: a local, soft darkening ──
    def _shade(self, img: np.ndarray, d: float) -> np.ndarray:
        if d <= 0.01:
            return img
        rows = np.linspace(0, 1, H, dtype=np.float32)
        local = np.exp(-((rows - self.center_y) / 0.20) ** 2)
        scale = 1 - d * (0.18 + 0.52 * local)
        return (img.astype(np.float32) * scale.reshape(H, 1, 1)).astype(np.uint8)

    def draw(self, img: np.ndarray, t: float) -> np.ndarray:
        active = [i for i, p in enumerate(self.pages) if p.t_in - 0.5 <= t <= p.t_out + OUT_S + 0.5]
        if not active:
            return img
        d = max(self._page_state(i, t) for i in active)
        img = self._shade(img, d)
        u = min(1.0, t / self.hold_end)
        push = _ease(u) if u < 1 else 1.0
        scale = 1.0 + 0.05 * push
        shift_y = (self.center_y - 0.5) * H * 0.17 * 0.8 * push + 4 * math.sin(t * 0.9)
        shift_x = 7 * math.sin(t * 0.55 + 1.0)
        env = self.env(t)
        entrance = self.look.entrance
        in_s = ENTRANCE_S.get(entrance, 0.5)
        for i in active:
            page = self.pages[i]
            leave = t - page.t_out
            self._rule(img, i, t, shift_x, shift_y)
            for p, (glow, line_y) in zip(self.patches[i], self.extra[i]):
                a_in = _ease((t - p.word.t0) / in_s)
                if a_in <= 0:
                    continue
                a, dy, dx, sigma, pop = a_in, 0.0, 0.0, 0.0, 0.0
                if entrance == "rise":
                    dy, sigma = (1 - a_in) * 30.0, (1 - a_in) * 8.0
                elif entrance == "slam":
                    a = min(1.0, a_in * 2.5)
                    pop = 0.55 * (1 - a_in) ** 2             # arrives large and settles
                else:                                        # track
                    dx, sigma = -(1 - a_in) * 44.0, (1 - a_in) * 4.0
                lift = 0.0
                if leave > 0:
                    k = _ease((leave - 0.06 * p.line) / OUT_S)
                    a *= 1 - k
                    lift = k
                    dy -= k * 36.0
                    sigma = max(sigma, k * 9.0)
                if a <= 0.01:
                    continue
                self._draw_word(img, p, glow, line_y, a, a_in, dy + shift_y, sigma, scale * (1 + pop), shift_x + dx, env, t, lift, i)
        return img

    # ── the line beside / above the block ──
    def _rule(self, img: np.ndarray, i: int, t: float, shift_x: float, shift_y: float) -> None:
        kind = self.look.rule
        if kind == "none" or not self.bounds[i][2]:
            return
        page = self.pages[i]
        x0, y0, x1, y1 = self.bounds[i]
        grow = _ease((t - (page.t_in - 0.1)) / 0.7)
        a = grow * (1 - _ease((t - page.t_out) / OUT_S))
        if a <= 0.02:
            return
        col = np.array(self.accent, np.float32)
        sx, sy = int(shift_x), int(shift_y)
        if kind == "bar":
            x = x0 - 34 + sx
            top, length = y0 + 6 + sy, int((y1 - y0 - 12) * grow)
            rect = (x, x + 6, top, top + length)
        else:                                               # top: a short line above a centred block, growing from the middle
            half = int(90 * grow)
            y = y0 - 38 + sy
            rect = (W // 2 - half + sx, W // 2 + half + sx, y, y + 4)
        xa, xb, ya, yb = max(0, rect[0]), min(W, rect[1]), max(0, rect[2]), min(H, rect[3])
        if xb <= xa or yb <= ya:
            return
        gy0, gy1, gx0, gx1 = max(0, ya - 8), min(H, yb + 8), max(0, xa - 8), min(W, xb + 8)
        halo = img[gy0:gy1, gx0:gx1].astype(np.float32) + col * 0.10 * a            # a faint light around the line
        img[gy0:gy1, gx0:gx1] = np.clip(halo, 0, 255).astype(np.uint8)
        roi = img[ya:yb, xa:xb].astype(np.float32)
        img[ya:yb, xa:xb] = np.clip(roi * (1 - a) + col * a, 0, 255).astype(np.uint8)

    def _draw_word(self, img, p, glow, line_y, a, a_in, dy, sigma, scale, shift_x, env, t, lift, page_i) -> None:
        mask, shadow = p.mask, p.shadow
        accent = p.word.accent
        gl = glow if accent else None
        h0, w0 = mask.shape
        cx, cy = p.x + w0 / 2, p.y + h0 / 2
        if abs(scale - 1) > 0.004:
            nw, nh = max(2, int(w0 * scale)), max(2, int(h0 * scale))
            mask = cv2.resize(mask, (nw, nh), interpolation=cv2.INTER_LINEAR)
            shadow = cv2.resize(shadow, (nw, nh), interpolation=cv2.INTER_LINEAR)
            if gl is not None:
                gl = cv2.resize(gl, (nw, nh), interpolation=cv2.INTER_LINEAR)
            # the block grows about its own middle, so a word keeps its place in the line
            bx0, by0, bx1, by1 = self.bounds[page_i]
            mx, my = (bx0 + bx1) / 2, (by0 + by1) / 2
            cx = mx + (cx - mx) * scale
            cy = my + (cy - my) * scale
        if sigma > 0.6:
            mask = cv2.GaussianBlur(mask, (0, 0), sigma)
            shadow = cv2.GaussianBlur(shadow, (0, 0), sigma)
        h, w = mask.shape
        x0, y0 = int(round(cx - w / 2 + shift_x)), int(round(cy - h / 2 + dy))
        xa, ya = max(0, x0), max(0, y0)
        xb, yb = min(W, x0 + w), min(H, y0 + h)
        if xb <= xa or yb <= ya:
            return
        sl = (slice(ya - y0, yb - y0), slice(xa - x0, xb - x0))
        roi = img[ya:yb, xa:xb].astype(np.float32)
        roi *= (1 - shadow[sl] * a * 0.75)[..., None]
        base = np.array(p.color, np.float32)
        if accent:                                        # lit from within: near-white letters, the colour is in the glow
            base = base * 0.3 + WHITE * 0.7
        if gl is not None:                                # the key words glow, in time with the voice
            g = (gl[sl] * a * (0.22 + 0.40 * env))[..., None]
            roi = roi + np.array(self.accent, np.float32) * g
        m = (mask[sl] * a)[..., None]
        if a_in < 0.999:                                   # a band of light crosses the letters once as they arrive
            xs = np.linspace(0, 1, w, dtype=np.float32)[sl[1]]
            band = np.exp(-((xs - (a_in * 1.5 - 0.25)) / 0.13) ** 2)[None, :, None] * 0.85
            colour = base * (1 - band) + WHITE * band
            roi = roi * (1 - m) + colour * m
        else:
            roi = roi * (1 - m) + base * m
        if lift > 0:                                       # leaving: the letters burn out to light
            roi = roi + mask[sl][..., None] * 90.0 * lift * a
        if accent and a_in > 0.2:                          # the underline draws itself while the word is spoken
            prog = _smooth((t - p.word.t0) / max(0.25, p.word.t1 - p.word.t0 + 0.3))
            ly = int(line_y * scale)
            span = int(w * 0.84 * prog)
            if span > 2 and 0 <= ly < h - 4:
                lx0 = int(w * 0.08) - (xa - x0)
                lo, hi = max(0, lx0), min(xb - xa, lx0 + span)
                ya2, yb2 = ly - (ya - y0), ly - (ya - y0) + 4
                if hi > lo and 0 <= ya2 and yb2 <= yb - ya:
                    roi[ya2:yb2, lo:hi] = roi[ya2:yb2, lo:hi] * (1 - a) + np.array(self.accent, np.float32) * a
        img[ya:yb, xa:xb] = np.clip(roi, 0, 255).astype(np.uint8)

"""Backdrop layers: a mood laid over the whole picture (before the branding and text).

A layer is `Layer(kind, strength, t0, t1, fade)`; several can be active at once and a short can change
mood part-way (`film` for the first 10s, then `mono` + `rain`). Everything is plain numpy/OpenCV applied to
the finished BGR frame, so it works for every style and needs no extra package.

    mono      colour drains to black and white (with a little contrast)
    sepia     warm brown monochrome
    film      old film: grain, flicker, gate weave, scratches, dust, vignette, a warm cast
    rain      rain on glass: the picture fogs over and clear drops slide down it, each one a small inverted lens
    ripple    seen through slowly moving water: a low rolling displacement and a shimmer
    vignette  dark edges
    leak      a slow warm light leak
    dust      floating light specks
    embers    glowing sparks rising and swaying (in `color`)
    mist      slow drifting fog, lightening the picture
    aurora    soft coloured lights orbiting slowly (in `color`)
    bokeh     large out-of-focus lights drifting up at two depths, glowing a little brighter as the voice speaks
    rays      slow shafts of light from the top corner (in `color`) that swell with the voice
    frame     a hairline border inset from the edge that draws itself in, with a light running round it
    grain     fine film grain over everything, so the picture, the lights and the words share one texture
    snow      soft flakes falling at two depths
    petals    blossom petals (in `color`) tumbling down on a slow wind
    scan      scanlines, a slow bright scan bar and now and then a glitch band with a colour split
    brackets  four corner brackets that draw themselves in and breathe
    bars      cinema bars that slide in from the top and bottom edge, with a hairline in `color`

`parse("mono,rain:0.8@0-12,film@12-")` builds layers from a short text (CLI / app setting);
`for_emotion("sad", t0, t1)` picks the layers for a scene's mood.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

KINDS = ("mono", "sepia", "film", "rain", "ripple", "vignette", "leak", "dust", "embers", "mist", "aurora",
         "bokeh", "rays", "frame", "grain", "snow", "petals", "scan", "brackets", "bars")

# which layers go with which mood (pace names from pacing.py plus the tagger's usual expressions)
EMOTION_LOOKS: dict[str, list[tuple[str, float]]] = {
    "calm": [("vignette", 0.5), ("dust", 0.6)],
    "sad": [("mono", 0.85), ("rain", 0.8), ("vignette", 0.6)],
    "tense": [("vignette", 0.8), ("film", 0.4)],
    "action": [("vignette", 0.5), ("leak", 0.4)],
    "reveal": [("film", 0.5), ("leak", 0.6)],
    "comic": [("leak", 0.4)],
    "nostalgic": [("film", 0.9), ("sepia", 0.6)],
    "cold": [("ripple", 0.7), ("vignette", 0.5)],
    "calm_water": [("ripple", 0.5), ("vignette", 0.4)],
}
ALIASES = {"grief": "sad", "sorrow": "sad", "cry": "sad", "melancholy": "sad", "angry": "tense", "anger": "tense",
           "fear": "tense", "determined": "action", "fight": "action", "battle": "action", "serene": "calm",
           "peaceful": "calm", "memory": "nostalgic", "flashback": "nostalgic", "past": "nostalgic",
           "shock": "reveal", "surprise": "reveal", "cold": "cold", "stoic": "cold", "neutral": "calm"}


@dataclass
class Layer:
    kind: str
    strength: float = 1.0
    t0: float = 0.0
    t1: float = 1e9
    fade: float = 0.6                  # seconds to blend in and out at the ends
    color: tuple = (90, 170, 255)      # BGR, for embers / aurora

    def weight(self, t: float) -> float:
        if t < self.t0 or t > self.t1:
            return 0.0
        w = self.strength
        if self.fade > 0:
            w *= min(1.0, (t - self.t0) / self.fade if self.t0 > 0 else 1.0, (self.t1 - t) / self.fade)
        return max(0.0, min(1.0, w))


def parse(spec: str | None, length: float = 1e9) -> list[Layer]:
    """"mono,rain:0.8@0-12,film@12-" -> layers. `:s` is the strength, `@a-b` the seconds (open ends allowed)."""
    layers: list[Layer] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        t0, t1 = 0.0, length
        if "@" in part:
            part, span = part.split("@", 1)
            a, _, b = span.partition("-")
            t0 = float(a) if a.strip() else 0.0
            t1 = float(b) if b.strip() else length
        s = 1.0
        if ":" in part:
            part, sv = part.split(":", 1)
            s = float(sv)
        kind = part.strip().lower()
        if kind not in KINDS:
            raise ValueError(f"unknown backdrop '{kind}' (one of {', '.join(KINDS)})")
        layers.append(Layer(kind, s, t0, t1))
    return layers


def for_emotion(emotion: str | None, t0: float = 0.0, t1: float = 1e9) -> list[Layer]:
    key = (emotion or "calm").strip().lower()
    key = ALIASES.get(key, key)
    return [Layer(k, s, t0, t1) for k, s in EMOTION_LOOKS.get(key, EMOTION_LOOKS["calm"])]


class Backdrop:
    """Applies the layers to frames given in order (it keeps the film dust, the raindrops and so on between frames)."""

    def __init__(self, layers: list[Layer], w: int = 1080, h: int = 1920, seed: int = 11):
        self.layers = layers
        self.w, self.h = w, h
        self.rng = np.random.default_rng(seed)
        self._t_last: float | None = None
        self._vig: np.ndarray | None = None
        self._grid: tuple[np.ndarray, np.ndarray] | None = None
        self._drops: list[list[float]] = []      # x, y, r, speed, pause
        self._scratches: list[list[float]] = []  # x, frames left, strength
        self._specks: list[list[float]] = []     # x, y, vx, vy, r, phase
        self._sparks: list[list[float]] = []     # x, y, vx, vy, r, phase
        self._fog: np.ndarray | None = None
        self.env = None                          # env(t) -> 0..1, the loudness of the voice (amv.cinema.voice_envelope)
        self._bokeh: list[list[float]] = []      # x, y, r, speed, phase, depth
        self._flakes: list[list[float]] = []     # x, y, vy, r, phase, depth
        self._petals: list[list[float]] = []     # x, y, vy, size, phase, spin

    # ── helpers ──
    def _vignette(self) -> np.ndarray:
        if self._vig is None:
            ys, xs = np.mgrid[0:self.h, 0:self.w].astype(np.float32)
            d = np.sqrt(((xs - self.w / 2) / (self.w / 2)) ** 2 + ((ys - self.h / 2) / (self.h / 2)) ** 2)
            self._vig = np.clip((d - 0.55) / 0.85, 0, 1) ** 1.6
            self._vig = self._vig[..., None].astype(np.float32)
        return self._vig

    def _identity(self) -> tuple[np.ndarray, np.ndarray]:
        if self._grid is None:
            xs, ys = np.meshgrid(np.arange(self.w, dtype=np.float32), np.arange(self.h, dtype=np.float32))
            self._grid = (xs, ys)
        return self._grid

    # ── effects ──
    @staticmethod
    def _mono(img: np.ndarray, w: float) -> np.ndarray:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        g = np.clip((g - 128) * 1.1 + 128, 0, 255)
        g3 = np.repeat(g[..., None], 3, axis=2)
        return (img * (1 - w) + g3 * w).astype(np.uint8)

    @staticmethod
    def _sepia(img: np.ndarray, w: float) -> np.ndarray:
        m = np.array([[0.131, 0.534, 0.272], [0.168, 0.686, 0.349], [0.189, 0.769, 0.393]], np.float32)  # BGR rows
        s = np.clip(img.astype(np.float32) @ m.T, 0, 255)
        return (img * (1 - w) + s * w).astype(np.uint8)

    def _film(self, img: np.ndarray, w: float, t: float) -> np.ndarray:
        h, wd = img.shape[:2]
        f = img.astype(np.float32)
        # warm, slightly faded
        f = f * (1 - 0.18 * w) + 128 * 0.18 * w
        f[..., 0] *= 1 - 0.07 * w
        f[..., 2] *= 1 + 0.04 * w
        # flicker
        f *= 1 + float(self.rng.normal(0, 0.03)) * w
        # grain at half res so it is coarse like film
        g = self.rng.normal(0, 1, (h // 2, wd // 2)).astype(np.float32)
        g = cv2.resize(g, (wd, h), interpolation=cv2.INTER_LINEAR)
        f += (g * 13 * w)[..., None]
        # scratches
        if self.rng.random() < 0.35 * w and len(self._scratches) < 3:
            self._scratches.append([float(self.rng.integers(0, wd)), float(self.rng.integers(2, 5)), float(self.rng.uniform(0.25, 0.6))])
        for s in self._scratches:
            x = int(s[0] + self.rng.normal(0, 1.0))
            if 0 <= x < wd - 1:
                y0 = int(self.rng.integers(0, h // 4))
                f[y0:h - int(self.rng.integers(0, h // 4)), x:x + 1] += 255 * s[2] * w * 0.5
            s[1] -= 1
        self._scratches = [s for s in self._scratches if s[1] > 0]
        # dust
        for _ in range(int(self.rng.integers(0, 6))):
            x, y, r = int(self.rng.integers(0, wd)), int(self.rng.integers(0, h)), int(self.rng.integers(1, 4))
            col = 235.0 if self.rng.random() < 0.6 else 20.0
            cv2.circle(f, (x, y), r, (col, col, col), -1)
        # vignette
        f *= 1 - 0.55 * w * self._vignette()
        out = np.clip(f, 0, 255).astype(np.uint8)
        # gate weave: a pixel or two
        dx, dy = float(self.rng.normal(0, 1.2)) * w, float(self.rng.normal(0, 1.2)) * w
        M = np.float32([[1, 0, dx], [0, 1, dy]])
        return cv2.warpAffine(out, M, (wd, h), borderMode=cv2.BORDER_REFLECT)

    def _rain(self, img: np.ndarray, w: float, t: float, dt: float) -> np.ndarray:
        h, wd = img.shape[:2]
        target = int(26 * (0.4 + w))
        while len(self._drops) < target:
            r = float(self.rng.uniform(16, 46))
            self._drops.append([float(self.rng.uniform(0, wd)), float(self.rng.uniform(-60, h)), r,
                                float(self.rng.uniform(40, 220)) * (r / 30), float(self.rng.uniform(0, 1.5))])
        # frosted glass: the picture blurred, a little darker; the drops stay sharp
        small = cv2.resize(img, (wd // 6, h // 6), interpolation=cv2.INTER_AREA)
        small = cv2.GaussianBlur(small, (0, 0), 2.2)
        fog = cv2.resize(small, (wd, h), interpolation=cv2.INTER_CUBIC).astype(np.float32) * (1 - 0.12 * w)
        out = (img.astype(np.float32) * (1 - 0.8 * w) + fog * 0.8 * w).astype(np.uint8)
        gx, gy = self._identity()
        alive = []
        for d in self._drops:
            x, y, r, v, pause = d
            if pause > 0:                      # drops hang, then slide
                d[4] -= dt
            else:
                d[1] += v * dt
                if self.rng.random() < 0.02:
                    d[4] = float(self.rng.uniform(0.2, 1.0))
            if d[1] - r > h:
                continue                       # off the bottom: replaced above
            alive.append(d)
            x0, x1 = int(max(0, x - r)), int(min(wd, x + r + 1))
            y0, y1 = int(max(0, y - r * 1.25)), int(min(h, y + r * 1.25 + 1))
            if x1 - x0 < 4 or y1 - y0 < 4:
                continue
            ys, xs = np.mgrid[y0:y1, x0:x1].astype(np.float32)
            nx, ny = (xs - x) / r, (ys - y) / (r * 1.25)
            rr = nx * nx + ny * ny
            inside = rr < 1.0
            if not inside.any():
                continue
            # inverted lens: the drop shows the scene upside down and magnified
            mapx = x - nx * r * 0.7
            mapy = y - ny * r * 1.25 * 0.7
            view = cv2.remap(img, mapx.astype(np.float32), mapy.astype(np.float32), cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REFLECT).astype(np.float32)
            rim = np.clip((rr - 0.55) / 0.45, 0, 1)[..., None]
            view = view * (1 - 0.35 * rim)
            hl = np.exp(-(((nx + 0.35) ** 2 + (ny + 0.4) ** 2) / 0.02))[..., None] * 170
            view = np.clip(view + hl, 0, 255)
            a = (inside[..., None] * np.clip((1 - rr) * 6, 0, 1)[..., None] * w).astype(np.float32)
            roi = out[y0:y1, x0:x1].astype(np.float32)
            out[y0:y1, x0:x1] = (roi * (1 - a) + view * a).astype(np.uint8)
        self._drops = alive
        return out

    def _ripple(self, img: np.ndarray, w: float, t: float) -> np.ndarray:
        h, wd = img.shape[:2]
        qw, qh = wd // 4, h // 4
        ys, xs = np.mgrid[0:qh, 0:qw].astype(np.float32)
        u, v = xs / qw, ys / qh
        amp = 9.0 * w
        dx = np.sin(2 * math.pi * (v * 3.2 + t * 0.28) + np.sin(2 * math.pi * (u * 2 + t * 0.15)) * 1.4)
        dy = np.sin(2 * math.pi * (u * 2.6 - t * 0.22) + np.sin(2 * math.pi * (v * 2.4 - t * 0.12)) * 1.2)
        dx += 0.5 * np.sin(2 * math.pi * (v * 7 - t * 0.5) + u * 5)
        dy += 0.5 * np.sin(2 * math.pi * (u * 6 + t * 0.4) + v * 4)
        fx = cv2.resize(dx * amp, (wd, h), interpolation=cv2.INTER_CUBIC)
        fy = cv2.resize(dy * amp, (wd, h), interpolation=cv2.INTER_CUBIC)
        gx, gy = self._identity()
        out = cv2.remap(img, gx + fx, gy + fy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        # light catching the ripples
        shine = cv2.resize(np.clip(dx * dy, 0, 1), (wd, h), interpolation=cv2.INTER_LINEAR) * 38 * w
        out = np.clip(out.astype(np.float32) + shine[..., None], 0, 255)
        out[..., 0] *= 1 + 0.05 * w          # a cool cast
        return np.clip(out, 0, 255).astype(np.uint8)

    def _vignette_fx(self, img: np.ndarray, w: float) -> np.ndarray:
        return np.clip(img.astype(np.float32) * (1 - 0.6 * w * self._vignette()), 0, 255).astype(np.uint8)

    def _leak(self, img: np.ndarray, w: float, t: float) -> np.ndarray:
        h, wd = img.shape[:2]
        cx = wd * (0.5 + 0.55 * math.sin(t * 0.35))
        cy = h * (0.25 + 0.12 * math.sin(t * 0.21 + 1))
        ys, xs = np.mgrid[0:h:4, 0:wd:4].astype(np.float32)
        d = np.sqrt(((xs - cx) / (wd * 0.6)) ** 2 + ((ys - cy) / (h * 0.35)) ** 2)
        glow = np.exp(-d * d * 1.6)
        glow = cv2.resize(glow, (wd, h), interpolation=cv2.INTER_LINEAR)[..., None]
        col = np.array([40, 110, 255], np.float32)          # warm orange (BGR)
        return np.clip(img.astype(np.float32) + glow * col * 0.55 * w, 0, 255).astype(np.uint8)

    def _dust(self, img: np.ndarray, w: float, t: float, dt: float) -> np.ndarray:
        h, wd = img.shape[:2]
        while len(self._specks) < int(40 * w) + 10:
            self._specks.append([float(self.rng.uniform(0, wd)), float(self.rng.uniform(0, h)), float(self.rng.normal(8, 6)),
                                 float(self.rng.uniform(-18, -4)), float(self.rng.uniform(1.5, 4)), float(self.rng.uniform(0, 6.3))])
        layer = np.zeros((h, wd, 3), np.float32)
        for s in self._specks:
            s[0] = (s[0] + s[2] * dt) % wd
            s[1] = (s[1] + s[3] * dt) % h
            a = 0.35 + 0.35 * math.sin(t * 1.3 + s[5])
            cv2.circle(layer, (int(s[0]), int(s[1])), max(1, int(s[4])), (a * 255,) * 3, -1)
        layer = cv2.GaussianBlur(layer, (0, 0), 2.0)
        return np.clip(img.astype(np.float32) + layer * w, 0, 255).astype(np.uint8)

    def _embers(self, img: np.ndarray, w: float, t: float, dt: float, color: tuple) -> np.ndarray:
        h, wd = img.shape[:2]
        n = int(70 * w) + 12
        while len(self._sparks) < n:
            self._sparks.append([float(self.rng.uniform(0, wd)), float(self.rng.uniform(0, h)), float(self.rng.normal(0, 14)),
                                 float(self.rng.uniform(-230, -70)), float(self.rng.uniform(3, 11)), float(self.rng.uniform(0, 6.3))])
        q = 4
        layer = np.zeros((h // q, wd // q, 3), np.float32)
        col = np.array(color, np.float32) / 255.0
        for s in self._sparks:
            s[1] += s[3] * dt
            s[0] += (s[2] + 38 * math.sin(t * 1.6 + s[5])) * dt
            if s[1] < -30:
                s[1], s[0] = h + 20.0, float(self.rng.uniform(0, wd))
            a = 0.45 + 0.55 * math.sin(t * 4.0 + s[5]) ** 2
            cv2.circle(layer, (int(s[0] / q) % layer.shape[1], int(s[1] / q)), max(1, int(s[4] / q)), (a * col * 1.6).tolist(), -1)
        glow = cv2.GaussianBlur(layer, (0, 0), 1.6)
        up = cv2.resize(glow, (wd, h), interpolation=cv2.INTER_LINEAR) * 255 * 1.5
        return np.clip(img.astype(np.float32) + up * w, 0, 255).astype(np.uint8)

    def _mist(self, img: np.ndarray, w: float, t: float) -> np.ndarray:
        h, wd = img.shape[:2]
        qh, qw = h // 8, wd // 8
        if self._fog is None:
            f = self.rng.random((qh, qw * 2)).astype(np.float32)
            n = f.shape[1]
            f = cv2.GaussianBlur(np.tile(f, (1, 3)), (0, 0), 7)[:, n:2 * n]      # blurred over a tiling: seamless sideways
            self._fog = (f - f.min()) / max(1e-6, float(f.max() - f.min()))
        fog = self._fog
        s1 = int(t * 9) % fog.shape[1]
        s2 = int(t * 5) % fog.shape[1]
        a = np.roll(fog, -s1, axis=1)[:, :qw]
        b = np.roll(fog[::-1], s2, axis=1)[:, :qw]
        m = np.clip((a * 0.6 + b * 0.4 - 0.35) * 1.8, 0, 1)
        m = cv2.resize(m, (wd, h), interpolation=cv2.INTER_CUBIC)[..., None]
        return np.clip(img.astype(np.float32) + m * 70 * w, 0, 255).astype(np.uint8)

    def _aurora(self, img: np.ndarray, w: float, t: float, color: tuple) -> np.ndarray:
        h, wd = img.shape[:2]
        ys, xs = np.mgrid[0:h:8, 0:wd:8].astype(np.float32)
        glow = np.zeros_like(xs)
        for i in range(3):
            cx = wd * (0.5 + 0.45 * math.sin(t * 0.33 + i * 2.1))
            cy = h * (0.5 + 0.38 * math.cos(t * 0.24 + i * 1.7))
            d2 = ((xs - cx) ** 2 + (ys - cy) ** 2) / (wd * 0.55) ** 2
            glow += np.exp(-d2 * 1.4)
        glow = cv2.resize(glow, (wd, h), interpolation=cv2.INTER_LINEAR)[..., None]
        col = np.array(color, np.float32)
        return np.clip(img.astype(np.float32) + glow * col * 0.42 * w, 0, 255).astype(np.uint8)

    def _voice(self, t: float) -> float:
        return float(self.env(t)) if self.env else 0.0

    def _bokeh_fx(self, img: np.ndarray, w: float, t: float, dt: float, color: tuple) -> np.ndarray:
        h, wd = img.shape[:2]
        if not self._bokeh:
            for i in range(16):
                depth = float(self.rng.choice([0.6, 1.0]))
                self._bokeh.append([float(self.rng.uniform(0, wd)), float(self.rng.uniform(0, h)),
                                    float(self.rng.uniform(34, 70) * (1.0 if depth < 1 else 1.7)),
                                    float(self.rng.uniform(14, 34)) * depth, float(self.rng.uniform(0, 6.3)), depth])
        q = 4
        layer = np.zeros((h // q, wd // q, 3), np.float32)
        col = np.array(color, np.float32) / 255.0 * 0.55 + 0.45
        boost = 0.8 + 0.6 * self._voice(t)
        for b in self._bokeh:
            b[1] -= b[3] * dt
            b[0] += 12 * math.sin(t * 0.4 + b[4]) * dt
            if b[1] < -b[2]:
                b[1], b[0] = h + b[2], float(self.rng.uniform(0, wd))
            a = (0.16 + 0.10 * math.sin(t * 0.8 + b[4])) * boost * (0.7 + 0.3 * b[5])
            c = (int(b[0] / q) % layer.shape[1], int(b[1] / q))
            r = max(2, int(b[2] / q))
            cv2.circle(layer, c, r, (col * a).tolist(), -1, cv2.LINE_AA)
            cv2.circle(layer, c, r, (col * a * 1.9).tolist(), 1, cv2.LINE_AA)      # the bright rim of a real bokeh disc
        layer = cv2.GaussianBlur(layer, (0, 0), 1.3)
        up = cv2.resize(layer, (wd, h), interpolation=cv2.INTER_LINEAR) * 255
        return np.clip(img.astype(np.float32) + up * w, 0, 255).astype(np.uint8)

    def _rays(self, img: np.ndarray, w: float, t: float, color: tuple) -> np.ndarray:
        h, wd = img.shape[:2]
        ys, xs = np.mgrid[0:h:8, 0:wd:8].astype(np.float32)
        ox, oy = wd * (-0.12 + 0.05 * math.sin(t * 0.21)), h * -0.10
        ang = np.arctan2(ys - oy, xs - ox)
        dist = np.sqrt((xs - ox) ** 2 + (ys - oy) ** 2) / h
        band = 0.5 + 0.5 * np.sin(ang * 17 + t * 0.35 + 1.7 * np.sin(ang * 5 - t * 0.2))
        shaft = band ** 5 * np.clip(1.15 - dist, 0, 1) ** 1.6
        shaft = cv2.resize(shaft, (wd, h), interpolation=cv2.INTER_LINEAR)[..., None]
        col = np.array(color, np.float32) * 0.55 + np.array([120, 150, 175], np.float32) * 0.45
        k = 0.34 * w * (0.7 + 0.6 * self._voice(t))
        return np.clip(img.astype(np.float32) + shaft * col * k, 0, 255).astype(np.uint8)

    def _frame_fx(self, img: np.ndarray, w: float, t: float, t0: float, color: tuple) -> np.ndarray:
        h, wd = img.shape[:2]
        m = 30
        pts = [(m, m), (wd - m, m), (wd - m, h - m), (m, h - m)]
        per = [wd - 2 * m, h - 2 * m, wd - 2 * m, h - 2 * m]
        total = float(sum(per))
        layer = np.zeros((h // 2, wd // 2, 3), np.uint8)
        draw_to = min(1.0, max(0.0, (t - t0) / 1.4))
        draw_to = 1 - (1 - draw_to) ** 3
        walked, col = 0.0, tuple(int(c) for c in color)
        light = ((t - t0) * 0.16) % 1.0
        for i in range(4):
            a, b = pts[i], pts[(i + 1) % 4]
            f0, f1 = walked / total, (walked + per[i]) / total
            walked += per[i]
            if draw_to <= f0:
                break
            f = min(1.0, (draw_to - f0) / (f1 - f0))
            end = (int((a[0] + (b[0] - a[0]) * f) / 2), int((a[1] + (b[1] - a[1]) * f) / 2))
            cv2.line(layer, (a[0] // 2, a[1] // 2), end, tuple(int(c * 0.55) for c in col), 1, cv2.LINE_AA)
            if draw_to >= 1.0:                                 # a light runs round the border
                d = (light * total - (f0 * total)) / per[i]
                if 0 <= d <= 1:
                    c = (int((a[0] + (b[0] - a[0]) * d) / 2), int((a[1] + (b[1] - a[1]) * d) / 2))
                    cv2.circle(layer, c, 3, (255, 255, 255), -1, cv2.LINE_AA)
                    cv2.circle(layer, c, 9, col, 1, cv2.LINE_AA)
        glow = cv2.GaussianBlur(layer, (0, 0), 3.0)
        up = cv2.resize(cv2.add(layer, glow), (wd, h), interpolation=cv2.INTER_LINEAR)
        return cv2.add(img, (up * min(1.0, w)).astype(np.uint8))

    def _snow(self, img: np.ndarray, w: float, t: float, dt: float) -> np.ndarray:
        h, wd = img.shape[:2]
        if not self._flakes:
            for _ in range(110):
                d = float(self.rng.choice([0.55, 1.0]))
                self._flakes.append([float(self.rng.uniform(0, wd)), float(self.rng.uniform(0, h)), float(self.rng.uniform(55, 130)) * d,
                                     float(self.rng.uniform(2.0, 5.5)) * (0.7 + 0.5 * d), float(self.rng.uniform(0, 6.3)), d])
        q = 2
        layer = np.zeros((h // q, wd // q, 3), np.float32)
        for f in self._flakes:
            f[1] += f[2] * dt
            f[0] += (22 * math.sin(t * 0.7 + f[4]) + 14) * dt * f[5]
            if f[1] > h + 10:
                f[1], f[0] = -10.0, float(self.rng.uniform(0, wd))
            f[0] %= wd
            a = 0.5 + 0.4 * f[5]
            cv2.circle(layer, (int(f[0] / q), int(f[1] / q)), max(1, int(f[3] / q)), (a, a, a), -1, cv2.LINE_AA)
        layer = cv2.GaussianBlur(layer, (0, 0), 1.1)
        up = cv2.resize(layer, (wd, h), interpolation=cv2.INTER_LINEAR) * 255
        return np.clip(img.astype(np.float32) + up * w, 0, 255).astype(np.uint8)

    def _petals_fx(self, img: np.ndarray, w: float, t: float, dt: float, color: tuple) -> np.ndarray:
        h, wd = img.shape[:2]
        if not self._petals:
            for _ in range(30):
                self._petals.append([float(self.rng.uniform(0, wd)), float(self.rng.uniform(-h * 0.1, h)), float(self.rng.uniform(50, 110)),
                                     float(self.rng.uniform(9, 20)), float(self.rng.uniform(0, 6.3)), float(self.rng.uniform(-90, 90))])
        q = 2
        layer = np.zeros((h // q, wd // q, 3), np.float32)
        col = np.array(color, np.float32) / 255.0 * 0.6 + 0.4
        for p in self._petals:
            p[1] += p[2] * dt
            p[0] += (70 + 46 * math.sin(t * 0.9 + p[4])) * dt           # carried sideways by a slow wind
            if p[1] > h + 30 or p[0] > wd + 30:
                p[1], p[0] = -30.0, float(self.rng.uniform(-wd * 0.3, wd))
            ang = (t * p[5] + p[4] * 57) % 360
            squash = 0.35 + 0.65 * abs(math.sin(t * 1.3 + p[4]))          # it tumbles: the ellipse turns edge-on and back
            axes = (max(1, int(p[3] / q)), max(1, int(p[3] / q * squash)))
            cv2.ellipse(layer, (int(p[0] / q), int(p[1] / q)), axes, ang, 0, 360, (col * 0.95).tolist(), -1, cv2.LINE_AA)
        layer = cv2.GaussianBlur(layer, (0, 0), 0.9)
        up = cv2.resize(layer, (wd, h), interpolation=cv2.INTER_LINEAR) * 255
        return np.clip(img.astype(np.float32) + up * w, 0, 255).astype(np.uint8)

    def _scan(self, img: np.ndarray, w: float, t: float, color: tuple) -> np.ndarray:
        h, wd = img.shape[:2]
        out = img.astype(np.float32)
        rows = np.ones(h, np.float32)
        rows[::4] -= 0.16 * w                                           # scanlines
        out *= rows.reshape(h, 1, 1)
        bar_y = int(((t * 0.18) % 1.25 - 0.125) * h)                    # a soft bright bar sweeping down
        if -120 < bar_y < h:
            y0, y1 = max(0, bar_y), min(h, bar_y + 110)
            fall = np.sin(np.linspace(0, math.pi, max(1, y1 - y0)), dtype=np.float32) ** 2
            out[y0:y1] += fall.reshape(-1, 1, 1) * np.array(color, np.float32) * 0.16 * w
        slot = int(t * 7)
        if (slot * 2654435761) % 11 < 2:                                # a glitch now and then: a band slips sideways, colours split
            r = np.random.default_rng(slot)
            y0 = int(r.integers(0, h - 140)); bh = int(r.integers(30, 110)); dx = int(r.integers(-26, 27)) or 14
            band = out[y0:y0 + bh].copy()
            band[..., 2] = np.roll(band[..., 2], dx, axis=1)
            band[..., 0] = np.roll(band[..., 0], -dx, axis=1)
            out[y0:y0 + bh] = band
        return np.clip(out, 0, 255).astype(np.uint8)

    def _brackets(self, img: np.ndarray, w: float, t: float, t0: float, color: tuple) -> np.ndarray:
        h, wd = img.shape[:2]
        m, ln = 38, 96 + int(8 * math.sin((t - t0) * 1.6))
        k = 1 - (1 - min(1.0, max(0.0, (t - t0) / 1.0))) ** 3
        L = int(ln * k)
        if L < 2:
            return img
        layer = np.zeros((h // 2, wd // 2, 3), np.uint8)
        col = tuple(int(c) for c in color)
        for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
            cx, cy = (m if sx > 0 else wd - m) // 2, (m if sy > 0 else h - m) // 2
            cv2.line(layer, (cx, cy), (cx + sx * L // 2, cy), col, 2, cv2.LINE_AA)
            cv2.line(layer, (cx, cy), (cx, cy + sy * L // 2), col, 2, cv2.LINE_AA)
        glow = cv2.GaussianBlur(layer, (0, 0), 3.0)
        up = cv2.resize(cv2.add(layer, glow), (wd, h), interpolation=cv2.INTER_LINEAR)
        return cv2.add(img, (up * min(1.0, w)).astype(np.uint8))

    def _bars(self, img: np.ndarray, w: float, t: float, t0: float, color: tuple) -> np.ndarray:
        h, wd = img.shape[:2]
        bar = 96
        k = 1 - (1 - min(1.0, max(0.0, (t - t0) / 0.9))) ** 3
        b = int(bar * k * min(1.0, w))
        if b < 1:
            return img
        img = img.copy()
        img[:b] = (img[:b].astype(np.float32) * 0.08).astype(np.uint8)
        img[h - b:] = (img[h - b:].astype(np.float32) * 0.08).astype(np.uint8)
        c = np.array([int(v * 0.8) for v in color], np.uint8)
        img[max(0, b - 2):b] = c
        img[h - b:h - b + 2] = c
        return img

    def _grain(self, img: np.ndarray, w: float) -> np.ndarray:
        h, wd = img.shape[:2]
        g = self.rng.normal(0, 1, (h // 2, wd // 2)).astype(np.float32)
        g = cv2.resize(g, (wd, h), interpolation=cv2.INTER_LINEAR)
        return np.clip(img.astype(np.int16) + (g * 7.5 * w).astype(np.int16)[..., None], 0, 255).astype(np.uint8)

    # ── entry ──
    def apply(self, img: np.ndarray, t: float) -> np.ndarray:
        if not self.layers:
            return img
        dt = 1 / 30 if self._t_last is None else max(0.0, min(0.2, t - self._t_last))
        self._t_last = t
        for lay in self.layers:
            w = lay.weight(t)
            if w <= 0.01:
                continue
            k = lay.kind
            if k == "mono":
                img = self._mono(img, w)
            elif k == "sepia":
                img = self._sepia(img, w)
            elif k == "film":
                img = self._film(img, w, t)
            elif k == "rain":
                img = self._rain(img, w, t, dt)
            elif k == "ripple":
                img = self._ripple(img, w, t)
            elif k == "vignette":
                img = self._vignette_fx(img, w)
            elif k == "leak":
                img = self._leak(img, w, t)
            elif k == "dust":
                img = self._dust(img, w, t, dt)
            elif k == "embers":
                img = self._embers(img, w, t, dt, lay.color)
            elif k == "mist":
                img = self._mist(img, w, t)
            elif k == "aurora":
                img = self._aurora(img, w * (0.75 + 0.5 * self._voice(t)), t, lay.color)
            elif k == "bokeh":
                img = self._bokeh_fx(img, w, t, dt, lay.color)
            elif k == "rays":
                img = self._rays(img, w, t, lay.color)
            elif k == "frame":
                img = self._frame_fx(img, w, t, lay.t0, lay.color)
            elif k == "grain":
                img = self._grain(img, w)
            elif k == "snow":
                img = self._snow(img, w, t, dt)
            elif k == "petals":
                img = self._petals_fx(img, w, t, dt, lay.color)
            elif k == "scan":
                img = self._scan(img, w, t, lay.color)
            elif k == "brackets":
                img = self._brackets(img, w, t, lay.t0, lay.color)
            elif k == "bars":
                img = self._bars(img, w, t, lay.t0, lay.color)
        return img

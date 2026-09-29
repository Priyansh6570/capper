"""Animated ReCapper splash screen shown while the Flask server boots.

Mirrors the "ReCapper Splash" Claude Design canvas (the "Signal" variant:
960x600, 28px rounded corners, black+red, orbiting ring + breathing glow).
There is no browser/webview available yet at this point in startup (that is
the whole reason this window exists - see tray_launcher.py), so the design
is re-rendered natively: every frame is composited in Pillow (already a hard
dependency - see requirements.txt) into one image, blitted onto a single Tk
label. No new runtime/dependency is introduced for this.

Run as a SEPARATE PROCESS, not imported in-process by tray_launcher.py (see
its own _show_splash_and_wait): tkinter/Tcl can crash natively (observed in
the wild - Windows Error Reporting APPCRASH in tcl86t.dll, exception
0x80000003), which no Python try/except can catch since the whole process
dies before any exception machinery runs. That used to take the Flask server
down with it (they shared a process). Isolating this into its own process
means a splash crash can only kill the splash.

CLI: `python splash.py <host> <port> <logo_path> <min_s> <max_s>` - blocks
until the target port is accepting connections AND at least `min_s` has
elapsed, then fades out and exits. Always exits 0, even on its own internal
error - see show_and_wait().
"""
from __future__ import annotations

import math
import os
import socket
import time
from pathlib import Path

W, H = 560, 350
RADIUS = 30

BG_TOP = (11, 11, 13)
BG_BOTTOM = (7, 7, 8)
ACCENT = (255, 43, 58)
TEXT_BRIGHT = (247, 244, 240)
TEXT_DIM = (154, 150, 145)
TEXT_FAINT = (79, 76, 72)
TEXT_STATUS = (142, 138, 133)
TRACK_BG = (21, 21, 26)
TRANSPARENT_KEY = "#ff00fe"  # arbitrary key color, not used anywhere else in the design

CENTER = (W // 2, 149)
GLOW_DIAMETER = 222
RING_R = 72
RING_R2 = 56
LOGO_SIZE = 86

MESSAGES = ["Waking the cat", "Loading panel reader", "Warming up narrator",
            "Preparing timeline", "Ready"]

FRAME_MS = 50  # ~20fps - smooth enough for the breathe/spin/fade, cheap enough to never stutter


def _ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def _entrance(t: float, delay: float, dur: float) -> float:
    """0..1 progress of a one-shot entrance animation that starts at `delay`."""
    if t < delay:
        return 0.0
    return max(0.0, min(1.0, (t - delay) / dur))


def _fade_color(rgb: tuple, alpha_frac: float) -> tuple:
    return rgb + (int(255 * max(0.0, min(1.0, alpha_frac))),)


def _vertical_gradient(w: int, h: int, top: tuple, bottom: tuple):
    from PIL import Image

    strip = Image.new("RGB", (1, h))
    px = strip.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return strip.resize((w, h)).convert("RGBA")


def _rounded_mask(w: int, h: int, r: int):
    from PIL import Image, ImageDraw

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    return mask


def _glow_sprite(diameter: int):
    from PIL import Image

    img = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    px = img.load()
    c = diameter / 2
    for y in range(diameter):
        for x in range(diameter):
            d = math.hypot(x - c, y - c) / c
            if d >= 1:
                continue
            px[x, y] = ACCENT + (int(255 * (1 - d) ** 2),)
    return img


def _beam_sprite(w: int, h: int):
    from PIL import Image

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        d = abs(t - 0.5) * 2
        a = int(14 * (1 - d))
        if a <= 0:
            continue
        for x in range(w):
            px[x, y] = (255, 60, 70, a)
    return img


def _load_font(names: list, size: int):
    from PIL import ImageFont

    fonts_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in names:
        try:
            return ImageFont.truetype(str(fonts_dir / name), size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def _tracked_text(draw, xy, text: str, font, fill, tracking: int = 0, center: bool = True) -> None:
    """PIL has no letter-spacing - draw glyph by glyph with extra advance."""
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = xy[0] - total / 2 if center else xy[0]
    for ch, wch in zip(text, widths):
        draw.text((x, xy[1]), ch, font=font, fill=fill, anchor="lm")
        x += wch + tracking


def _dashed_circle(draw, center, r, rotation_deg, fill, width, dash=8, gap=8) -> None:
    cx, cy = center
    circumference = 2 * math.pi * r
    n = max(1, int(circumference / (dash + gap)))
    step_deg = 360.0 / n
    dash_deg = step_deg * (dash / (dash + gap))
    for i in range(n):
        s = rotation_deg + i * step_deg
        draw.arc([cx - r, cy - r, cx + r, cy + r], start=s, end=s + dash_deg, fill=fill, width=width)


def _run(host: str, port: int, logo_path: Path, min_s: float, max_s: float) -> None:
    import tkinter as tk
    import sys
    from PIL import Image, ImageDraw, ImageTk

    rounded = sys.platform.startswith("win")

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    bg_key = TRANSPARENT_KEY if rounded else "#070708"
    root.configure(bg=bg_key)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")
    if rounded:
        try:
            root.attributes("-transparentcolor", bg_key)
        except tk.TclError:
            rounded = False

    label = tk.Label(root, bd=0, highlightthickness=0, bg=bg_key)
    label.pack(fill="both", expand=True)

    mask = _rounded_mask(W, H, RADIUS) if rounded else None
    base_gradient = _vertical_gradient(W, H, BG_TOP, BG_BOTTOM)
    glow_sprite = _glow_sprite(GLOW_DIAMETER)
    beam_sprite = _beam_sprite(W, int(H * 0.4))

    logo_img = None
    try:
        if logo_path.exists():
            logo_img = Image.open(logo_path).convert("RGBA")
            logo_img.thumbnail((LOGO_SIZE, LOGO_SIZE), Image.LANCZOS)
    except Exception:  # noqa: BLE001
        logo_img = None

    title_font = _load_font(["segoeuisl.ttf", "segoeui.ttf", "arial.ttf"], 26)
    mono_font = _load_font(["consola.ttf", "cour.ttf", "arial.ttf"], 10)
    tag_font = _load_font(["consola.ttf", "cour.ttf", "arial.ttf"], 10)

    start = time.monotonic()

    def _port_open() -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            return False

    def render(t: float, progress: float, message: str, cursor_on: bool):
        frame = base_gradient.copy()

        breathe = 0.5 + 0.5 * math.sin(t * (2 * math.pi / 4.0) - math.pi / 2)
        scale = 1.0 + 0.12 * breathe
        opacity = 0.35 + 0.35 * breathe
        gsize = max(1, int(GLOW_DIAMETER * scale))
        g = glow_sprite.resize((gsize, gsize), Image.BILINEAR)
        a = g.split()[3].point(lambda v: int(v * opacity))
        g.putalpha(a)
        frame.alpha_composite(g, (CENTER[0] - gsize // 2, CENTER[1] - gsize // 2))

        cycle = 7.0
        phase = (t % cycle) / cycle
        by = int(-beam_sprite.height + phase * (H + beam_sprite.height))
        frame.alpha_composite(beam_sprite, (0, by))

        draw = ImageDraw.Draw(frame, "RGBA")

        ring_p = _entrance(t, 0.2, 1.0)
        if ring_p > 0:
            spin = (t / 3.0) * 360.0 % 360.0
            col = _fade_color(ACCENT, ring_p)
            draw.arc([CENTER[0] - RING_R, CENTER[1] - RING_R, CENTER[0] + RING_R, CENTER[1] + RING_R],
                      start=spin, end=spin + 88, fill=col, width=2)
            rev = (-t / 9.0) * 360.0 % 360.0
            _dashed_circle(draw, CENTER, RING_R2, rev, fill=_fade_color((42, 42, 48), ring_p * 0.6), width=1)

        logo_p = _entrance(t, 0.35, 1.0)
        if logo_img is not None and logo_p > 0:
            off = int((1 - _ease_out(logo_p)) * 11)
            li = logo_img
            if logo_p < 1.0:
                li = li.copy()
                li.putalpha(li.split()[3].point(lambda v: int(v * logo_p)))
            frame.alpha_composite(li, (CENTER[0] - li.width // 2, CENTER[1] - li.height // 2 + off))

        title_p = _entrance(t, 0.7, 0.9)
        if title_p > 0:
            off = int((1 - _ease_out(title_p)) * 12)
            _tracked_text(draw, (W // 2, 245 + off), "RECAPPER", title_font,
                          _fade_color(TEXT_BRIGHT, title_p), tracking=5)

        tag_p = _entrance(t, 1.1, 0.9)
        if tag_p > 0:
            _tracked_text(draw, (W // 2, 278), "EVERY CHAPTER, RECAPPED.", tag_font,
                          _fade_color(TEXT_DIM, tag_p), tracking=2)

        meta_p = _entrance(t, 1.4, 1.0)
        if meta_p > 0:
            draw.text((19, H - 26), "RECAPPER \u00b7 v1.0.0", font=mono_font,
                      fill=_fade_color(TEXT_FAINT, meta_p))
            sw_ = draw.textlength(message, font=mono_font)
            sx = W - 19 - sw_ - 11
            draw.text((sx, H - 26), message, font=mono_font, fill=_fade_color(TEXT_STATUS, meta_p))
            if cursor_on:
                draw.text((W - 19 - 9, H - 26), "\u2588", font=mono_font, fill=_fade_color(ACCENT, meta_p))

        draw.rectangle([0, H - 2, W, H], fill=TRACK_BG)
        fw = int(W * max(0.0, min(1.0, progress)))
        if fw > 0:
            draw.rectangle([0, H - 2, fw, H], fill=ACCENT)

        if mask is not None:
            frame.putalpha(mask)
        return frame

    photo_ref = {"img": None}

    def fade_out(alpha: float = 1.0) -> None:
        try:
            root.attributes("-alpha", alpha)
        except tk.TclError:
            try:
                root.destroy()
            except tk.TclError:
                pass
            return
        if alpha <= 0.0:
            try:
                root.destroy()
            except tk.TclError:
                pass
            return
        root.after(20, lambda: fade_out(alpha - 0.1))

    def tick() -> None:
        elapsed = time.monotonic() - start
        ready = _port_open()
        done = (ready and elapsed >= min_s) or elapsed >= max_s

        raw = min(1.0, elapsed / max(min_s, 0.001))
        eased = 1 - (1 - raw) ** 2
        progress = 1.0 if done else 0.92 * eased
        idx = min(len(MESSAGES) - 1, int(raw * len(MESSAGES)))
        message = MESSAGES[-1] if done else MESSAGES[idx]
        cursor_on = int(elapsed * 2) % 2 == 0

        try:
            frame = render(elapsed, progress, message, cursor_on)
            photo = ImageTk.PhotoImage(frame)
            photo_ref["img"] = photo
            label.configure(image=photo)
        except tk.TclError:
            return

        if done:
            fade_out()
            return
        root.after(FRAME_MS, tick)

    root.after(10, tick)
    root.mainloop()


def show_and_wait(host: str, port: int, logo_path: Path, *, min_s: float = 3.0, max_s: float = 8.0) -> None:
    try:
        _run(host, port, logo_path, min_s, max_s)
    except Exception:  # noqa: BLE001 - a broken splash must never block the app from starting
        pass


if __name__ == "__main__":
    import sys
    _, host, port, logo_path, min_s, max_s = sys.argv
    show_and_wait(host, int(port), Path(logo_path), min_s=float(min_s), max_s=float(max_s))

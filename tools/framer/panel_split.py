"""Split a stitched chapter strip into reading-order panels at its gutters.

A gutter is a run of rows that are near-uniform in colour. Panels keep the
strip's full width and are cut from the full-resolution image, never resized.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from manifest import Panel

UNIFORM_STD = 6.0           # a row whose pixel std is below this counts as uniform
MIN_GUTTER_FRAC = 0.012     # shortest uniform run that is a gutter, as a fraction of width
MIN_PANEL_FRAC = 0.06       # panels shorter than this fraction of width are slivers
MIN_INK = 0.05              # panels with a smaller share of non-uniform rows are blank
MAX_PANEL_FRAC = 2.0        # panels taller than this fraction of width get split
BAND_ROWS = 2000            # rows measured at a time, to bound memory


def _row_std(strip: Image.Image) -> np.ndarray:
    bands = []
    for y in range(0, strip.height, BAND_ROWS):
        band = np.asarray(strip.crop((0, y, strip.width, min(y + BAND_ROWS, strip.height))))
        bands.append(band.reshape(len(band), -1).std(axis=1))
    return np.concatenate(bands)


def _content_ranges(uniform: np.ndarray, min_gutter: int) -> list[tuple[int, int]]:
    edges = np.flatnonzero(np.diff(np.concatenate(([0], uniform.astype(np.int8), [0])))).reshape(-1, 2)
    ranges, start = [], 0
    for gutter_start, gutter_end in edges:
        if gutter_end - gutter_start < min_gutter:
            continue
        if gutter_start > start:
            ranges.append((start, int(gutter_start)))
        start = int(gutter_end)
    if start < len(uniform):
        ranges.append((start, len(uniform)))
    return ranges


def _split_tall(top: int, bottom: int, row_std: np.ndarray, max_height: int) -> list[tuple[int, int]]:
    if bottom - top <= max_height:
        return [(top, bottom)]
    span = bottom - top
    lo, hi = top + span * 2 // 5, top + span * 3 // 5
    cut = lo + int(np.argmin(row_std[lo:hi]))
    return _split_tall(top, cut, row_std, max_height) + _split_tall(cut, bottom, row_std, max_height)


def split_panels(strip_path: Path, out_dir: Path) -> list[Panel]:
    """Write P001.png, P002.png, ... into `out_dir` and return them as Panels
    whose bbox is the panel's [x, y, w, h] on the strip."""
    strip = Image.open(strip_path)
    width = strip.width
    row_std = _row_std(strip)
    uniform = row_std < UNIFORM_STD

    ranges = []
    for top, bottom in _content_ranges(uniform, max(2, round(width * MIN_GUTTER_FRAC))):
        if bottom - top < width * MIN_PANEL_FRAC or (~uniform[top:bottom]).mean() < MIN_INK:
            continue
        ranges += _split_tall(top, bottom, row_std, round(width * MAX_PANEL_FRAC))
    if not ranges:
        raise ValueError("No panels found in the strip.")

    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("P*.png"):
        stale.unlink()
    panels = []
    for order, (top, bottom) in enumerate(ranges, start=1):
        panel_id = f"P{order:03d}"
        path = out_dir / f"{panel_id}.png"
        strip.crop((0, top, width, bottom)).save(path)
        panels.append(Panel(id=panel_id, bbox=[0.0, float(top), float(width), float(bottom - top)],
                            order=order, crop=str(path)))
    return panels

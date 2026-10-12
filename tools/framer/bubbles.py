"""Local speech-bubble detector: finds light or dark rounded regions that enclose
text-sized glyphs, so blur boxes land exactly on the bubble instead of on a
model's guess."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

LIGHT, DARK = 225, 40       # grey levels beyond which a pixel counts as light / dark bubble paper
MIN_AREA_FRAC = 0.004       # smallest bubble, as a fraction of panel width squared
MAX_AREA_FRAC = 0.5         # larger regions are backgrounds, as a fraction of the panel
MIN_FILL = 0.7              # blob area / bounding box area: bubbles are round-ish, not ragged
MIN_GLYPHS = 3              # glyph-sized holes inside the blob; a flat highlight has none
MAX_GLYPH_FRAC = 0.3        # a hole taller than this share of the blob is shape, not a letter
INK_RANGE = (0.02, 0.5)     # share of the blob covered by glyphs
PAD = 6                     # px added around the detected box


def _bubbles_in(mask: np.ndarray, width: int, height: int, keep_inside: bool) -> list[list[int]]:
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for index, contour in enumerate(contours):
        if hierarchy[0][index][3] != -1:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)
        if not MIN_AREA_FRAC * width ** 2 <= area <= MAX_AREA_FRAC * width * height or area < MIN_FILL * w * h:
            continue
        if keep_inside and (x == 0 or y == 0 or x + w >= width or y + h >= height):
            continue
        holes = [cv2.boundingRect(c) + (cv2.contourArea(c),) for i, c in enumerate(contours)
                 if hierarchy[0][i][3] == index]
        glyphs = [hole for hole in holes if hole[3] <= MAX_GLYPH_FRAC * h and hole[4] > 4]
        if len(glyphs) >= MIN_GLYPHS and INK_RANGE[0] <= sum(g[4] for g in glyphs) / area <= INK_RANGE[1]:
            x0, y0 = max(0, x - PAD), max(0, y - PAD)
            boxes.append([x0, y0, min(width, x + w + PAD) - x0, min(height, y + h + PAD) - y0])
    return boxes


def find_bubbles(panel_path: Path) -> list[list[int]]:
    """[x, y, w, h] of every bubble in the panel image, in that image's pixels."""
    gray = cv2.imread(str(panel_path), cv2.IMREAD_GRAYSCALE)
    height, width = gray.shape
    return (_bubbles_in((gray > LIGHT).astype(np.uint8), width, height, keep_inside=False)
            + _bubbles_in((gray < DARK).astype(np.uint8), width, height, keep_inside=True))

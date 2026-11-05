"""The script step's quality modes: how much of each panel Claude is shown.

Full sends every panel as an image; Balanced sends smaller images plus the text read from them;
Lite sends only that text and no images.
"""
from __future__ import annotations

FULL, BALANCED, LITE = "full", "balanced", "lite"
MODES = (FULL, BALANCED, LITE)
LABELS = {FULL: "Full", BALANCED: "Balanced", LITE: "Lite"}
DEFAULT = FULL

MAX_IMAGE_SIDE = 2576       # Claude shrinks any image whose long edge is longer than this
FULL_SIDE = 1024
DEFAULT_BALANCED_SIDE = 512
MIN_BALANCED_SIDE = 256
BATCH_PANELS = {FULL: 40, BALANCED: 60, LITE: 200}   # panels per pass-1 call; fewer calls where less is sent


def normalize(mode) -> str | None:
    return mode if mode in MODES else None


def balanced_side(value) -> int:
    try:
        side = int(value)
    except (TypeError, ValueError):
        return DEFAULT_BALANCED_SIDE
    return max(MIN_BALANCED_SIDE, min(MAX_IMAGE_SIDE, side))


def image_side(mode: str, balanced: int) -> int | None:
    """Long edge of the panel copies sent in `mode`, None when it sends no images."""
    return {FULL: FULL_SIDE, BALANCED: balanced_side(balanced), LITE: None}[mode]

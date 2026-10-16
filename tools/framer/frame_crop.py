"""Exact frame rectangles from a model's rough choice of what to show.

The model names a subject and roughly where it sits in a panel. This module turns that into
the crop: it keeps the subject, cuts detected speech bubbles out wherever that costs the
subject nothing (a bubble that cannot be cut out stays in the frame, unaltered), then grows the
crop toward the video's aspect ratio and up to a minimum size, inside the panel and never into
a bubble it cut out. All rectangles are (x0, y0, x1, y1) in strip pixels.
"""
from __future__ import annotations

Rect = tuple[float, float, float, float]

VIDEO_ASPECT = 16 / 9
OUTPUT_SIZE = (1920, 1080)
SUBJECT_PAD = 0.08          # margin kept around the subject, as a share of its size
MAX_UPSCALE = 1.6           # a frame is never enlarged more than this to fill the output


def _width(r: Rect) -> float:
    return r[2] - r[0]


def _height(r: Rect) -> float:
    return r[3] - r[1]


def _overlaps(a: Rect, b: Rect) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _contains(outer: Rect, inner: Rect) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _clamp(r: Rect, bounds: Rect) -> Rect:
    return (max(r[0], bounds[0]), max(r[1], bounds[1]), min(r[2], bounds[2]), min(r[3], bounds[3]))


def _pad(r: Rect, share: float) -> Rect:
    dx, dy = _width(r) * share, _height(r) * share
    return (r[0] - dx, r[1] - dy, r[2] + dx, r[3] + dy)


def overlap_share(a: Rect, b: Rect) -> float:
    """Share of the smaller rectangle that the other covers."""
    inter = _clamp(a, b)
    area = max(0.0, _width(inter)) * max(0.0, _height(inter))
    return area / min(_width(a) * _height(a), _width(b) * _height(b))


def _cut_out(crop: Rect, subject: Rect, bubble: Rect) -> Rect | None:
    """The largest crop inside `crop` that keeps `subject` and excludes `bubble`, if any."""
    options = [(crop[0], crop[1], bubble[0], crop[3]), (bubble[2], crop[1], crop[2], crop[3]),
               (crop[0], crop[1], crop[2], bubble[1]), (crop[0], bubble[3], crop[2], crop[3])]
    kept = [o for o in options if _width(o) > 0 and _height(o) > 0 and _contains(o, subject)]
    return max(kept, key=lambda o: _width(o) * _height(o), default=None)


def _grow_axis(low: float, high: float, floor: float, ceiling: float, want: float) -> tuple[float, float]:
    """Extend [low, high] toward length `want` within [floor, ceiling], evenly where it can."""
    extra = want - (high - low)
    if extra <= 0:
        return low, high
    before = min(extra / 2, low - floor)
    after = min(extra - before, ceiling - high)
    before = min(extra - after, low - floor)
    return low - before, high + after


def _grow(crop: Rect, panel: Rect, obstacles: list[Rect], want_w: float, want_h: float) -> Rect:
    def blocked_x(o: Rect) -> bool:
        return o[1] < crop[3] and crop[1] < o[3]

    floor = max([panel[0]] + [o[2] for o in obstacles if blocked_x(o) and o[2] <= crop[0]])
    ceiling = min([panel[2]] + [o[0] for o in obstacles if blocked_x(o) and o[0] >= crop[2]])
    x0, x1 = _grow_axis(crop[0], crop[2], floor, ceiling, want_w)

    def blocked_y(o: Rect) -> bool:
        return o[0] < x1 and x0 < o[2]

    floor = max([panel[1]] + [o[3] for o in obstacles if blocked_y(o) and o[3] <= crop[1]])
    ceiling = min([panel[3]] + [o[1] for o in obstacles if blocked_y(o) and o[1] >= crop[3]])
    y0, y1 = _grow_axis(crop[1], crop[3], floor, ceiling, want_h)
    return (x0, y0, x1, y1)


def frame_for(panel: Rect, subject: Rect | None, bubbles: list[Rect]) -> Rect:
    """The crop for a subject inside `panel`; None shows the whole panel."""
    subject = _clamp(subject, panel) if subject else panel
    crop = _clamp(_pad(subject, SUBJECT_PAD), panel)
    for bubble in sorted(bubbles, key=lambda b: -_width(b) * _height(b)):
        if _overlaps(crop, bubble):
            crop = _cut_out(crop, subject, bubble) or crop
    obstacles = [b for b in bubbles if not _overlaps(crop, b)]

    want_w = max(_width(crop), _height(crop) * VIDEO_ASPECT)
    fit = min(OUTPUT_SIZE[0] / want_w, OUTPUT_SIZE[1] / _height(crop))
    enlarge = max(1.0, fit / MAX_UPSCALE)
    return _grow(crop, panel, obstacles, want_w * enlarge, _height(crop) * enlarge)

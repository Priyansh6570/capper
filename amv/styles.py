"""Style recipes: how a short is laid out over the music, and which bank frames go where.

A style is a list of sections over the track; each section has a mode that says how its cuts are timed,
which frames fill them and how they move (the renderer reads `Cut.mode` / `Cut.trans`):

    aura   cuts on the beat (hook / intro / build / drop / post), character frames, push + zoom-blur + flash
    story  slow cuts (>= STORY_HOLD seconds), frames in reading order, a drift and a dissolve between them
    burst  fast cuts on the beats after the drop, as `aura`'s drop/post part

    1 `aura`    aura over the whole track
    2 `story`   story until the drop, then a burst of the reveal
    3 `twist`   story, the twist on the last story cut, then aura from the drop
    4 `pivot`   aura, but only frames that show one focus point (eye, mouth, hand, weapon); the point is pinned to
                the same spot on screen in every shot and all the motion (push, zoom-blur, flash) happens around it

`build(style, items, a, start, length, drop_rel)` returns a Plan; the renderer takes it as it is. Items are the
frame bank's ({"path", "tag", "ch", "id", "order", "line"}, see amv/bank.py). Nothing here touches the manifest.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image

from . import planner
from .render import H, POINTS, W, Cut, FPS, build_cuts, pivot_of, thin_cuts

STORY_HOLD = 2.0         # a story frame stays at least this long (seconds)
WHIP_EVERY = 3           # in aura cuts, every n-th relaxed cut is a whip instead of the zoom-through


@dataclass
class Plan:
    cuts: list[Cut]
    frames: list[dict]                  # {"path", "tag"}: the pool the renderer loads
    order: list[int] | None = None      # pool index per cut; None = the renderer's aura order (hero last)
    hero: int = 0


STYLES = {
    "aura": "1 - character aura montage on the beat",
    "story": "2 - a slow story, then a fast reveal on the drop",
    "twist": "3 - a slow story, the twist before the drop, then aura on the drop",
    "pivot": "4 - aura around one fixed point of focus (--point eye|mouth|hand|weapon)",
}
MIN_PIVOT_FRAMES = 8     # fewer frames with the point than this cannot fill a short without repeating


def _snap(t: float) -> float:
    return round(round(t * FPS) / FPS, 4)


def _reading(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda x: (str(x.get("ch", "")), x.get("order", 0), x["id"]))


def _usable(items: list[dict]) -> list[dict]:
    """Frames fit for a short: no readable lettering."""
    return [x for x in items if not x["tag"].get("text")] or list(items)


def _sample(seq: list[dict], n: int) -> list[dict]:
    """`n` frames spread evenly over `seq` (kept in order); fewer when `seq` is shorter."""
    if n >= len(seq):
        return list(seq)
    return [seq[min(len(seq) - 1, int((i + 0.5) * len(seq) / n))] for i in range(n)]


def _strongest(seq: list[dict]) -> dict:
    return max(seq, key=lambda x: (x["tag"].get("intensity", 0), bool(x["tag"].get("has_face"))))


def _story_cuts(end: float, beats: list[float] | None = None) -> list[Cut]:
    """Holds of at least STORY_HOLD seconds from 0 to `end`, about equal, each change on a beat of the track."""
    n = max(1, int(end // (STORY_HOLD + 0.35)))              # a little slack, so an edge can move onto a beat
    ideal = [end * i / n for i in range(n)]
    edges = [0.0]
    for t in ideal[1:]:
        lo = edges[-1] + STORY_HOLD                              # this hold must last, and so must every later one
        hi = end - (n - len(edges)) * STORY_HOLD
        ok = [x for x in beats or [] if lo - 1e-6 <= x <= hi + 1e-6]
        edges.append(min(ok, key=lambda x: abs(x - t)) if ok else min(hi, max(lo, t)))
    edges = [_snap(t) for t in edges] + [_snap(end)]
    return [Cut(edges[i], edges[i + 1], "story", mode="story", trans="dissolve") for i in range(n)]


def _tail(cuts: list[Cut], drop_rel: float) -> list[Cut]:
    return [c for c in cuts if c.t0 >= drop_rel - 0.03]


def _whips(cuts: list[Cut]) -> None:
    """Every WHIP_EVERY-th relaxed aura cut whips in (a directional blur slide) instead of the zoom-through."""
    k = 0
    for c in cuts:
        if c.hero or c.kind not in ("intro", "post", "drop") or c.t1 - c.t0 < 0.3:
            continue
        k += 1
        if k % WHIP_EVERY == 0:
            c.trans = "whip"


def aura(items: list[dict], a: dict, start: float, length: float, drop_rel: float, point: str = "eye") -> Plan:
    cuts = thin_cuts(build_cuts(a, start, length, drop_rel), len(items))
    _whips(cuts)
    frames, hero = planner.plan_aura(items, cuts)
    return Plan(cuts, frames, None, hero)


def _burst(cuts: list[Cut]) -> list[Cut]:
    for c in cuts:
        c.mode = "burst"
    return cuts


def _beats_rel(a: dict, start: float, length: float) -> list[float]:
    return [b - start for b in a["beats"] if start <= b < start + length]


def story(items: list[dict], a: dict, start: float, length: float, drop_rel: float, point: str = "eye") -> Plan:
    """Story frames in reading order until the drop; from the drop the reveal bursts, on the strongest frame."""
    pool = _reading(_usable(items))
    scenes = _story_cuts(drop_rel, _beats_rel(a, start, length))
    burst = _burst(_tail(build_cuts(a, start, length, drop_rel), drop_rel))
    n_story, n_burst = len(scenes), len(burst)
    reveal = pool[-max(n_burst, 1):] if len(pool) > n_story else pool[-1:]
    head = [x for x in pool if x not in reveal] or pool
    story_frames = _sample(head, n_story)
    hero = _strongest(reveal)
    rest = [x for x in reveal if x is not hero]
    burst_frames = [hero] + [rest[i % len(rest)] for i in range(n_burst - 1)] if rest else [hero] * n_burst
    return _assemble(scenes + burst, story_frames + burst_frames)


def twist(items: list[dict], a: dict, start: float, length: float, drop_rel: float, point: str = "eye") -> Plan:
    """Story until just before the drop, the twist on the last story cut, then the aura montage from the drop on."""
    pool = _reading(_usable(items))
    scenes = _story_cuts(drop_rel, _beats_rel(a, start, length))
    n_story = len(scenes)
    late = pool[len(pool) * 2 // 3:] or pool
    twist_frame = _strongest(late)
    head = [x for x in pool if x is not twist_frame] or pool
    story_frames = _sample(head, n_story - 1) + [twist_frame] if n_story > 1 else [twist_frame]
    faces = [x for x in items if x not in story_frames] or items
    post = _tail(build_cuts(a, start, length, drop_rel), drop_rel)
    _whips(post)
    frames, hero_i = planner.plan_aura(faces, post)
    hero_frame = frames[hero_i]
    ordered = [hero_frame] + [f for i, f in enumerate(frames) if i != hero_i]   # the drop cut is first and is the hero
    return _assemble(scenes + post, story_frames + ordered[: len(post)] + [ordered[-1]] * max(0, len(post) - len(ordered)))


def _pinnable(path: str) -> bool:
    """A frame big enough to be zoomed about its point without going soft: it fills the screen, or it is a small
    panel the renderer letterboxes at no more than 2x."""
    try:
        with Image.open(path) as im:
            w, h = im.size
    except OSError:
        return False
    return max(W / w, H / h) <= 2.4 or W / w <= 2.0


def pivot(items: list[dict], a: dict, start: float, length: float, drop_rel: float, point: str = "eye") -> Plan:
    """The aura timing over only the frames that show `point`. Every one is treated as a face frame (the planner
    arranges by intensity) and the renderer pins the point at its anchor: no whips, which would slide it away."""
    if point not in POINTS:
        raise ValueError(f"unknown point {point!r}; choose from {', '.join(POINTS)}")
    usable = [x for x in _usable(items) if pivot_of(x["tag"], point) and _pinnable(x["path"])]
    if len(usable) < MIN_PIVOT_FRAMES:
        raise ValueError(f"only {len(usable)} frame(s) in the bank show the {point}; need at least {MIN_PIVOT_FRAMES}")
    cuts = thin_cuts(build_cuts(a, start, length, drop_rel), len(usable))
    as_faces = [{**x, "tag": {**x["tag"], "has_face": True, "kind": "face"}} for x in usable]
    frames, hero = planner.plan_aura(as_faces, cuts, nonface_share=0.0)
    real = {x["path"]: x for x in usable}
    return Plan(cuts, [{"path": f["path"], "tag": real[f["path"]]["tag"]} for f in frames], None, hero)


def _assemble(cuts: list[Cut], per_cut: list[dict]) -> Plan:
    """One frame per cut -> a pool of the distinct frames and each cut's index into it."""
    pool: list[dict] = []
    index: dict[str, int] = {}
    order = []
    for f in per_cut:
        if f["path"] not in index:
            index[f["path"]] = len(pool)
            pool.append({"path": f["path"], "tag": f["tag"]})
        order.append(index[f["path"]])
    cuts[-1].t1 = max(cuts[-1].t1, cuts[-1].t0 + 0.1)
    return Plan(cuts, pool, order, 0)


_BUILDERS = {"aura": aura, "story": story, "twist": twist, "pivot": pivot}


def build(style: str, items: list[dict], a: dict, start: float, length: float, drop_rel: float, point: str = "eye") -> Plan:
    if style not in _BUILDERS:
        raise ValueError(f"unknown style {style!r}; choose from {', '.join(_BUILDERS)}")
    if not items:
        raise ValueError("the frame bank has no frames")
    return _BUILDERS[style](items, a, start, length, drop_rel, point)

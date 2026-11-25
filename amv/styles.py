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
    5 `adaptive` reads the music's sections and uses the recipes above inside one short, with a mood backdrop per scene

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
    frames: list[dict]                  # {"path", "tag"[, "point"]}: the pool the renderer loads
    order: list[int] | None = None      # pool index per cut; None = the renderer's aura order (hero last)
    hero: int = 0
    overlay: object | None = None       # has .draw(img, t) -> img: words on screen (quote / lyrics)
    voice: tuple | None = None          # (wav, seconds into the clip it starts, seconds it is heard)
    voice_more: list | None = None      # further voices of the same shape (the closing message), mixed the same way
    backdrop: list | None = None        # mood layers this recipe wants when none were asked for
    post: list | None = None            # layers applied after the words (grain, border), so they share one texture
    env: object | None = None           # env(t) -> 0..1: the loudness of the voice; the lights of the backdrop follow it
    cover_only: bool = False            # every picture fills the whole screen (cropped), never letterboxed


STYLES = {
    "aura": "1 - character aura montage on the beat",
    "story": "2 - a slow story, then a fast reveal on the drop",
    "twist": "3 - a slow story, the twist before the drop, then aura on the drop",
    "pivot": "4 - aura around one fixed point of focus (--point eye|mouth|hand|weapon)",
    "adaptive": "5 - reads the music's sections: story before the drop, aura on it, pivot in a long drop, story in a break; a mood backdrop per scene",
    "lyrics": "7 - the track's lyrics on screen (timed LRC) over one hero frame of the protagonist, aura edit from the drop",
    "quote": "6 - a deep voice speaks the protagonist's quote over the music, words on screen, protagonist aura on the drop",
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


# -- 6: quote --
MOOD_WORDS = {
    "calm": ("calm", "serene", "peace", "quiet", "gentle", "compos", "thought", "contempl", "closed eyes", "relax", "soft"),
    "sad": ("sad", "sorrow", "grief", "tear", "cry", "melanch", "pain", "regret", "lonel", "downcast", "mourn", "somber"),
    "tense": ("tense", "serious", "stern", "glare", "intense", "focus", "wary", "grim", "anger", "angry", "cold", "resolve"),
    "action": ("determin", "fierce", "rage", "shout", "roar", "fight", "battle", "grin", "fury", "resolve", "yell"),
    "reveal": ("shock", "surpris", "wide", "stun", "realiz", "smirk", "confident", "astonish", "disbelief"),
    "cold": ("cold", "stoic", "expressionless", "emotionless", "icy", "impassive", "indifferent", "stern", "calm"),
    "nostalgic": ("wistful", "nostalg", "reminisc", "longing", "gentle", "soft", "smile", "fond", "tender"),
}


def protagonist(items: list[dict]) -> str | None:
    """The character with the most face frames in the bank (the tagger names a character when the context tells it)."""
    count: dict[str, int] = {}
    for x in items:
        t = x["tag"]
        name = (t.get("character") or "").strip().lower()
        if name and t.get("has_face") and t.get("solo", True):
            count[name] = count.get(name, 0) + 1
    return max(count, key=count.get) if count else None


def _cover_scale(path: str) -> float:
    """How much the frame must be magnified to fill the whole screen (1 = it already does, 3 = soft)."""
    try:
        with Image.open(path) as im:
            w, h = im.size
    except OSError:
        return 99.0
    return max(W / max(w, 1), H / max(h, 1))


FILL_BEST = 2.4          # up to here a frame fills the screen and still looks sharp
FILL_MAX = 3.4           # beyond this a frame is too small to fill the screen without going to mush


def _mood_hit(tag: dict, mood: str) -> bool:
    expr = (tag.get("expression") or "").lower()
    return any(k in expr for k in MOOD_WORDS.get(mood, ()))


def quote(items: list[dict], a: dict, start: float, length: float, drop_rel: float, point: str = "eye",
          extra: dict | None = None) -> Plan:
    """`extra`: {"quote": quote dict, "narration": voice.Narration, "voice_at": clip seconds, "character": name | None}.
    While the quote is spoken: one powerful frame of the protagonist held with a slow push, dimmed under the words. When it
    is done the main edit starts: the aura montage of his frames, the drop as its hero. The mood picks the frames, the accent colour and the backdrop."""
    from . import backdrop as bdrop, quotes as qmod, textlayer, voice
    extra = extra or {}
    q, narr = extra["quote"], extra.get("narration")
    mood = q.get("mood", "calm")
    labels = extra.get("labels") or set()                    # exact tag labels of the protagonist (amv.cast)
    who = (extra.get("character") or protagonist(items) or "").lower()
    mine = [x for x in items if x["tag"].get("has_face") and (
        (x["tag"].get("character") or "").strip() in labels if labels
        else who and who in (x["tag"].get("character") or "").lower())]
    if len(mine) < 8:
        mine = [x for x in items if x["tag"].get("has_face")] or items        # too few tagged as him: any faces
    usable = _usable(mine)
    # the pictures fill the whole screen (the renderer crops, it never letterboxes): keep the ones big enough to
    # do that without going soft, unless there are too few of them
    sharp = [x for x in usable if _cover_scale(x["path"]) <= FILL_MAX]
    if len(sharp) >= 8:
        usable = sharp

    def score(x: dict) -> float:
        t = x["tag"]
        return (2.0 if _mood_hit(t, mood) else 0.0) + (0.5 if t.get("visibility") == "full" else 0.0) + (0.3 if t.get("solo") else 0.0)

    from . import voice as vmod
    # while the quote is spoken (and its words leave): ONE powerful frame of the speaker, held with a slow push.
    # The main edit starts on the first beat-cut after that, and the drop is its hero.
    lyric_words = extra.get("words")                  # lyrics: (text, t0, t1, line) in clip time, no voice
    quote_end = (lyric_words[-1][2] if lyric_words else extra["voice_at"] + vmod.spoken_end(narr)) + 0.35
    outro = extra.get("outro")                        # the closing message: its own scene after the edit
    core = extra.get("core_len") or length            # where the edit ends and the message begins
    cuts_all = build_cuts(a, start, core, drop_rel)
    tail = [c for c in cuts_all if c.t0 >= quote_end - 0.03] or _tail(cuts_all, drop_rel)
    hold_end = tail[0].t0
    scene = Cut(0.0, hold_end, "hold", mode="hold", trans="dissolve")

    def power(x: dict) -> tuple:
        t = x["tag"]
        box = t.get("face_box") or [0, 0, 0, 0]
        return (_cover_scale(x["path"]) <= FILL_BEST, score(x), t.get("intensity", 5), box[3] if len(box) > 3 else 0)
    ranked = sorted(usable, key=power, reverse=True)
    hold = ranked[0]
    closing = next((x for x in ranked[1:] if x["path"] != hold["path"]), hold)      # the message gets a frame of its own
    rest = [x for x in usable if x is not hold] or usable
    boosted = [{**x, "tag": {**x["tag"], "has_face": True, "kind": "face",
                             "intensity": min(10, x["tag"].get("intensity", 5) + (2 if _mood_hit(x["tag"], mood) else 0))}}
               for x in rest]
    # the edit after the quote must never sit on one picture: with fewer of his frames than cuts, other sharp frames of
    # the chapter join the pool (other faces first, then the strongest scenes), and the cuts are thinned to what is left
    pool_all = list(rest)
    if len(pool_all) < len(tail):
        taken = {x["path"] for x in pool_all} | {hold["path"]}
        more = [x for x in items if x["path"] not in taken and not x["tag"].get("text")
                and _cover_scale(x["path"]) <= FILL_MAX]
        more.sort(key=lambda x: (bool(x["tag"].get("has_face")), x["tag"].get("intensity", 0)), reverse=True)
        pool_all += more[: len(tail) - len(pool_all)]
        boosted = [{**x, "tag": {**x["tag"], "has_face": True, "kind": "face",
                                 "intensity": min(10, x["tag"].get("intensity", 5) + (2 if _mood_hit(x["tag"], mood) else 0))}}
                   for x in pool_all]
    post = thin_cuts(tail, len(pool_all))
    _whips(post)
    frames, hero_i = planner.plan_aura(boosted, post, nonface_share=0.0)
    real = {x["path"]: x for x in pool_all}
    frames = [{"path": f["path"], "tag": real[f["path"]]["tag"]} for f in frames]
    ordered = [frames[hero_i]] + [f for i, f in enumerate(frames) if i != hero_i]
    per_cut = [hold] + [ordered[i % len(ordered)] for i in range(len(post))]     # a short pool cycles; it never freezes on one
    if outro:
        end_scene = Cut(core, length, "hold", mode="hold", trans="dissolve")
        plan = _assemble([scene] + post + [end_scene], per_cut + [closing])
    else:
        plan = _assemble([scene] + post, per_cut)

    if lyric_words:
        pages = textlayer.pages_from_words(lyric_words, qmod.emphasis_words(q), 0.0, by_sentence=True)
    else:
        words = [(w.text, w.t0, w.t1, w.sentence) for w in narr.words]
        # one page per sentence: the pause between sentences lets a page leave before the next arrives
        pages = textlayer.pages_from_words(words, qmod.emphasis_words(q), extra["voice_at"], per_page=18, by_sentence=True)
    from . import looks
    look = looks.get(extra.get("look"))
    # the quote's own colour wins; else the look's; else the mood's
    accent = qmod.accent_bgr(q) if (q.get("accent") or look.accent is None) else look.accent
    plan.cover_only = True
    if lyric_words:
        plan.overlay = textlayer.TextLayer(pages, accent=accent, font=q.get("font") or textlayer.DEFAULT_FONT)
    else:
        from . import cinema
        plan.env = cinema.voice_envelope(narr.wav, extra["voice_at"])
        plan.overlay = cinema.CinemaText(pages, accent=accent, font=q.get("font") or None, hold_end=hold_end,
                                         env=plan.env, look=look)
        if outro:
            n2 = outro["narration"]
            env2 = cinema.voice_envelope(n2.wav, outro["at"])
            env1 = plan.env
            plan.env = lambda t: max(env1(t), env2(t))
            words2 = [(w.text, w.t0, w.t1, w.sentence) for w in n2.words]
            pages2 = textlayer.pages_from_words(words2, {"playlist", "daily"}, outro["at"], per_page=18, by_sentence=True)
            closing_text = cinema.CinemaText(pages2, accent=accent, font=q.get("font") or None, hold_end=length,
                                             env=plan.env, look=look)
            plan.overlay = cinema.Stack(plan.overlay, closing_text)
    plan.voice = None if lyric_words else (str(narr.wav), extra["voice_at"], voice.spoken_end(narr) + 0.3)
    if outro and not lyric_words:
        plan.voice_more = [(str(outro["narration"].wav), outro["at"], voice.spoken_end(outro["narration"]) + 0.3)]
    live = hold_end + 0.3
    # the picture is alive (the look says how): lights that answer the voice, shafts, embers, snow, petals ...
    # while the quote is spoken, and the ones marked "all" for the whole short
    plan.backdrop = bdrop.for_emotion(mood, 0.0, length) + [
        bdrop.Layer(kind, strength, 0.0, length if scope == "all" else live, 0.8 if scope == "all" else 0.5, accent)
        for kind, strength, scope in look.layers]
    if outro:                                                   # the scene-only lights come back for the message
        plan.backdrop += [bdrop.Layer(kind, strength, core - 0.2, length, 0.5, accent)
                          for kind, strength, scope in look.layers if scope == "hold"]
    # after the words, over everything: the look's border (it draws itself in) and grain that binds picture, lights and type
    border = {"hairline": "frame", "brackets": "brackets", "bars": "bars"}.get(look.border)
    plan.post = ([bdrop.Layer(border, 0.8, 0.3, length, 0.0, accent)] if border else []) + [
        bdrop.Layer("grain", look.grain, 0.0, length, 0.0)]
    return plan


# -- 5: adaptive --
MIN_BREAK_S = 3.0        # a dip shorter than this is not worth a story scene
PIVOT_AFTER = 5.0        # a drop section this long (seconds past the drop) gets a pivot scene in its middle
PIVOT_LEN = 5.0


def _mean_low(a: dict, t0: float, t1: float) -> float:
    e = a.get("energy") or {}
    low, hop = e.get("low") or [], e.get("hop_s") or 0.0232
    seg = low[max(0, int(t0 / hop)): max(1, int(t1 / hop))]
    return float(sum(seg) / len(seg)) if len(seg) else 0.0


def sections(a: dict, start: float, length: float, drop_rel: float) -> list[tuple[float, float, str]]:
    """The music cut into (t0, t1, kind) in clip time: kind is story | aura | pivot.
    Before the drop: story when that part is quiet (a real build), else aura. A break after the drop becomes story.
    A long stretch of drop gets a pivot scene in the middle."""
    def snap(t: float) -> float:
        return _snap(min(max(t, 0.0), length))
    drop_rel = snap(drop_rel)
    out: list[tuple[float, float, str]] = []
    if drop_rel >= 2 * STORY_HOLD + 0.5:
        quiet = _mean_low(a, start, start + drop_rel) < 0.6 * max(_mean_low(a, start + drop_rel, start + length), 1e-9)
        out.append((0.0, drop_rel, "story" if quiet else "aura"))
    elif drop_rel > 0:
        out.append((0.0, drop_rel, "aura"))
    breaks = sorted((snap(b0 - start), snap(b1 - start)) for b0, b1 in a.get("breaks", [])
                    if drop_rel + 4.0 < b0 - start < length - 3.0 and b1 - b0 >= MIN_BREAK_S)
    cur = drop_rel
    for b0, b1 in breaks[:1]:                       # one break at most; more would make it a slideshow
        out += _drop_part(cur, b0)
        out.append((b0, min(b1, snap(length)), "story"))
        cur = min(b1, length)
    out += _drop_part(cur, snap(length))
    return [x for x in out if x[1] - x[0] > 0.2]


def _drop_part(t0: float, t1: float) -> list[tuple[float, float, str]]:
    if t1 - t0 >= PIVOT_AFTER + PIVOT_LEN + 3.0:
        p0 = _snap(t0 + PIVOT_AFTER)
        return [(t0, p0, "aura"), (p0, _snap(p0 + PIVOT_LEN), "pivot"), (_snap(p0 + PIVOT_LEN), t1, "aura")]
    return [(t0, t1, "aura")]


def _section_mood(frames: list[dict], default: str) -> str:
    votes = {m: sum(_mood_hit(f["tag"], m) for f in frames) for m in ("sad", "tense", "action", "reveal", "calm", "nostalgic")}
    best = max(votes, key=votes.get)
    return best if votes[best] >= max(2, len(frames) // 4) else default


def adaptive(items: list[dict], a: dict, start: float, length: float, drop_rel: float, point: str = "eye",
             extra: dict | None = None) -> Plan:
    """One short, several recipes: the music's sections decide (see `sections`), the section's frames decide its
    backdrop mood (`extra["mood"]` forces one). Frames are not reused across sections while the bank has spare ones."""
    from . import backdrop as bdrop
    extra = extra or {}
    plain = _usable(items)
    reading = _reading(plain)
    real = {x["path"]: x for x in plain}
    all_cuts = build_cuts(a, start, length, drop_rel)
    used: set[str] = set()
    cuts: list[Cut] = []
    per_cut: list[dict] = []
    layers: list = []

    def fresh(pool: list[dict], need: int) -> list[dict]:
        left = [x for x in pool if x["path"] not in used]
        return left if len(left) >= need else pool

    for t0, t1, kind in sections(a, start, length, drop_rel):
        if kind == "story":
            beats = [b for b in _beats_rel(a, start, length) if t0 <= b <= t1]
            sc = [Cut(c.t0 + t0, c.t1 + t0, "story", mode="story", trans="dissolve")
                  for c in _story_cuts(t1 - t0, [b - t0 for b in beats])]
            picks = _sample(fresh(reading, len(sc)), len(sc))
            picks = picks + [picks[-1]] * (len(sc) - len(picks))
            default = "calm"
        else:
            sc = [Cut(c.t0, c.t1, c.kind, c.downbeat, c.hero, c.mode, c.trans)
                  for c in all_cuts if t0 - 1e-6 <= c.t0 < t1 - 1e-6]
            if not sc:
                continue
            pt = None
            pool = [x for x in plain if x["tag"].get("has_face")] or plain
            if kind == "pivot":
                for cand in [point] + [p for p in POINTS if p != point]:
                    ok = [x for x in fresh(plain, 6) if pivot_of(x["tag"], cand) and _pinnable(x["path"])]
                    if len(ok) >= 6:
                        pt, pool = cand, [{**x, "tag": {**x["tag"], "has_face": True, "kind": "face"}} for x in ok]
                        break
            if pt is None:
                _whips(sc)
            pool = fresh(pool, len(sc))
            frames, hero_i = planner.plan_aura(pool, sc, nonface_share=0.0 if pt else 0.1)
            has_hero = any(c.hero for c in sc)
            seq = iter(frames[:-1] if has_hero else frames)
            picks = []
            for c in sc:
                f = frames[hero_i] if c.hero else next(seq, frames[0])
                base = real.get(f["path"], f)
                picks.append({**base, "tag": base["tag"], "point": pt} if pt else base)
            default = "action" if t0 >= drop_rel - 0.05 else "tense"
        sc[0].t0 = t0
        sc[-1].t1 = t1
        cuts += sc
        per_cut += picks
        used.update(x["path"] for x in picks)
        layers += bdrop.for_emotion(extra.get("mood") or _section_mood(picks, default), max(0.0, t0 - 0.3), t1 + 0.3)
    if not cuts:
        raise ValueError("adaptive: the window gave no sections")
    pool_out: list[dict] = []
    index: dict[tuple, int] = {}
    order: list[int] = []
    for f in per_cut:                                   # a frame is a distinct shot per focus point
        key = (f["path"], f.get("point"))
        if key not in index:
            index[key] = len(pool_out)
            pool_out.append({"path": f["path"], "tag": f["tag"], **({"point": f["point"]} if f.get("point") else {})})
        order.append(index[key])
    plan = Plan(cuts, pool_out, order, 0)
    plan.backdrop = layers
    return plan


_BUILDERS = {"aura": aura, "story": story, "twist": twist, "pivot": pivot}


def build(style: str, items: list[dict], a: dict, start: float, length: float, drop_rel: float, point: str = "eye",
          extra: dict | None = None) -> Plan:
    if style not in _BUILDERS and style not in ("quote", "lyrics", "adaptive"):
        raise ValueError(f"unknown style {style!r}; choose from {', '.join(list(_BUILDERS) + ['adaptive', 'quote', 'lyrics'])}")
    if not items:
        raise ValueError("the frame bank has no frames")
    if style == "adaptive":
        return adaptive(items, a, start, length, drop_rel, point, extra)
    if style in ("quote", "lyrics"):
        return quote(items, a, start, length, drop_rel, point, extra)
    return _BUILDERS[style](items, a, start, length, drop_rel, point)

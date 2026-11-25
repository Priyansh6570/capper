"""Make one quote short (style 6): voice the quote, find the part of the track where it ends just before the drop, render.

    voice  ->  window (start, end, drop)  ->  render(recipe="quote")

The narration ends `END_GAP` seconds before the drop (the words have left the screen by then), starts as early as the music
allows (`LEAD` seconds in) and the track plays from its beginning whenever the drop is close enough for that.
Files only: the voice wav is cached in cache/amv_voice, the frames come from the shorts bank, the result is an mp4.
"""
from __future__ import annotations

from pathlib import Path

from . import bank, cast, music, quotes as qmod, render, voice

LEAD = 0.8          # the quote starts this long after the clip starts (the music is heard first)
END_GAP = 0.55      # the last word ends this long before the drop
MAX_GAP = 6.0       # more silence than this before the voice and the clip starts later (on a phrase boundary) instead
MIN_DROP = 6.0      # an earlier "drop" is an intro hit
OUTRO_GAP = 0.45     # the closing message starts this long after the edit ends
OUTRO_TAIL = 1.1     # and the short ends this long after its last word
TAIL_S = 8.0        # the edit after the drop runs this long, then the short ends (the whole track is not needed)
MIN_LEN = 18.0      # but a short is never shorter than this


def plan_window(a: dict, spoken_end: float, length: float | None = None) -> tuple[float, float, float, float]:
    """(start, end, drop, voice_at): the first drop with room for the voice before it. `voice_at` is when the narration
    starts in the clip (clip seconds). Raises ValueError, naming the longest lead the track can give, when none fits."""
    need = LEAD + spoken_end + END_GAP
    drops = [d for d in a["drops"] if d >= MIN_DROP]
    grid = a["downbeats"] or a["beats"]
    bar = 60.0 / max(a["bpm"], 1.0) * 4
    origin = grid[0] if grid else 0.0
    best = 0.0
    for d in drops:
        d = music.snap(d, a["beats"])
        best = max(best, d)
        if d < need:
            continue
        if d - need <= MAX_GAP:
            start = 0.0                                   # the track from its beginning
        else:
            k = int((d - need - origin) // (bar * 2))      # latest 2-bar boundary that still leaves room
            start = max(0.0, origin + k * bar * 2)
        vat = max(LEAD, d - END_GAP - spoken_end - start)
        s, e, drop_rel = music.pick_window(a, length, drop=d, fixed_start=start)
        return s, e, s + drop_rel, vat
    raise ValueError(f"the quote needs {need:.1f}s before the drop, this track's drops come at {[round(d, 1) for d in drops]}: "
                     "use a shorter quote or another track")


def outro_text(series: str = "") -> str:
    """The warm closing message, spoken and shown at the end of every short."""
    name = " ".join((series or "").split())
    where = f"The playlist link for {name} is" if name else "The playlist link is"
    return f"Thank you for watching. {where} in our description and our pinned comment, and we upload daily."


def make(track: str | Path, bank_dirs: list[str | Path], quote: dict, out: str | Path, character: str | None = None,
         length: float | None = None, preview: bool = False, backdrop: str | None = None, tempo_scale: float = 1.0,
         progress=None, log=print, series: str = "", look: str | None = None, outro: bool = True) -> Path:
    items: list[dict] = []
    for work in bank_dirs:
        items += bank.load(Path(work))
    if not items:
        raise ValueError("no frames: give a chapter work folder with a built shorts bank")
    labels = cast.protagonist_labels(items, series, character or quote.get("character", ""), log=log)
    log(f"voicing: {quote['text']}")
    a = music.analyze(track, tempo_scale)
    plan = narr = None
    for speed in (voice.SPEED, 0.93, 1.0):          # a little faster rather than starting the track in the middle
        n = voice.narrate(quote["text"], speed=speed)
        try:
            p = plan_window(a, voice.spoken_end(n), length)
        except ValueError:
            if plan is None and speed == 1.0:
                raise
            continue
        if plan is None:
            plan, narr = p, n
        if p[0] == 0.0:
            plan, narr = p, n
            break
    if plan is None:
        raise ValueError("the quote does not fit before this track's drop")
    start, end, drop, vat = plan
    log(f"narration {voice.spoken_end(narr):.1f}s | clip {start:.1f}-{end:.1f}s of the track, drop at {drop - start:.1f}s, voice from {vat:.1f}s")
    clip = end - start
    if length is None:                                  # no need for the whole track: the drop, then a few seconds of edit
        clip = min(clip, max(MIN_LEN, (drop - start) + TAIL_S))
    extra = {"quote": quote, "narration": narr, "voice_at": vat, "character": character, "labels": labels, "look": look}
    if outro:
        text = outro_text(series)
        n2 = voice.narrate(text, voice=voice.PLAIN_VOICE, speed=1.0, pitch=1.0, plain=True)
        at = clip + OUTRO_GAP
        extra.update(core_len=clip, outro={"narration": n2, "at": at, "text": text})
        clip = at + voice.spoken_end(n2) + OUTRO_TAIL
        log(f"closing message from {at:.1f}s, the short is {clip:.1f}s: {text}")
    return render.render([], track, out, clip, tempo_scale, start, drop, 0, preview, progress=progress,
                         recipe="quote", items=items, backdrop=backdrop, extra=extra)

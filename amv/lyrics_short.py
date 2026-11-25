"""Make one lyrics short (style 7): the song's lyrics on screen until the drop, the protagonist after it.

Same look as the quote short (one hero frame with a living background and bold left-aligned words) but the words come from
the track's own lyrics, timed by an LRC file (`[mm:ss.xx] line`, times in the track), and there is no narration: the
music plays as it is. The lyrics stop `END_GAP` seconds before the drop; nothing is shown after it.
Files only: LRC (or plain lines) in, mp4 out.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import bank, cast, music, quotes as qmod, render

END_GAP = 0.55
LINE_MAX = 4.0           # a line stays at most this long
LRC = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """[(time in the track, line)] from LRC text; lines without a time stamp are ignored. A line may carry several stamps."""
    out: list[tuple[float, str]] = []
    for raw in text.splitlines():
        stamps = LRC.findall(raw)
        line = LRC.sub("", raw).strip()
        if stamps and line:
            out += [(int(m) * 60 + float(s), line) for m, s in stamps]
    return sorted(out)


def clip_words(lines: list[tuple[float, str]], start: float, drop_rel: float) -> list[tuple[str, float, float, int]]:
    """(word, t0, t1, line number) in clip time. Each line spreads its words over its own time (until the next line, at most
    LINE_MAX); everything that would pass `drop_rel - END_GAP` is cut."""
    limit = drop_rel - END_GAP
    words: list[tuple[str, float, float, int]] = []
    rel = [(t - start, s) for t, s in lines if t - start >= -0.05]
    for i, (t, line) in enumerate(rel):
        if t >= limit - 0.3:
            break
        nxt = rel[i + 1][0] if i + 1 < len(rel) else t + LINE_MAX
        end = min(t + LINE_MAX, nxt - 0.05, limit)
        parts = line.split()
        weights = [len(re.sub(r"\W", "", w)) + 1.5 for w in parts]
        total = sum(weights) or 1.0
        x = max(0.0, t)
        span = end - x
        for w, wt in zip(parts, weights):
            d = span * wt / total * 0.85            # the words arrive over the first part of the line, then it rests
            words.append((w, round(x, 3), round(x + d, 3), i))
            x += span * wt / total * 0.85
    return words


def make(track: str | Path, bank_dirs: list[str | Path], lyrics: str | Path, out: str | Path, mood: str = "tense",
         character: str | None = None, length: float | None = None, preview: bool = False, backdrop: str | None = None,
         tempo_scale: float = 1.0, progress=None, log=print, series: str = "", font: str = "") -> Path:
    items: list[dict] = []
    for work in bank_dirs:
        items += bank.load(Path(work))
    if not items:
        raise ValueError("no frames: give a chapter work folder with a built shorts bank")
    lines = parse_lrc(Path(lyrics).read_text(encoding="utf-8"))
    if not lines:
        raise ValueError("no timed lines in the lyrics file: use LRC ('[00:12.30] words of the line')")
    a = music.analyze(track, tempo_scale)
    drops = [d for d in a["drops"] if d >= 6.0] or a["drops"]
    first = lines[0][0]
    # the first drop that comes after the lyrics begin
    drop = next((d for d in drops if d > first + 3.0), drops[-1])
    start = 0.0 if drop <= 35 else max(0.0, first - 1.0)
    s, end, drop_rel = music.pick_window(a, length, drop=drop, fixed_start=start)
    words = clip_words(lines, s, drop_rel)
    if not words:
        raise ValueError(f"no lyrics fall between the clip start ({s:.1f}s) and the drop ({s + drop_rel:.1f}s)")
    log(f"clip {s:.1f}-{end:.1f}s of the track, drop at {drop_rel:.1f}s, {len(words)} words in {words[-1][3] + 1} lines")
    labels = cast.protagonist_labels(items, series, character or "", log=log)
    q = {"text": " ".join(w[0] for w in words), "mood": mood, "emphasis": [], "font": font}
    return render.render([], track, out, end - s, tempo_scale, s, s + drop_rel, 0, preview, progress=progress,
                         recipe="lyrics", items=items, backdrop=backdrop,
                         extra={"quote": q, "words": words, "character": character, "labels": labels})

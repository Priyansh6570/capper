"""Stage 2b / STAGE B - Frame matching (story-first).  Runs on: laptop (no LLM).

The pipeline is STORY-FIRST: STAGE A (s5) has already written the whole recap as
ordered SEGMENTS in `work/<chapter>/script/segments.json`, each carrying
`refers_to_orders` - the candidate panel ORDER numbers whose text fed that
segment. FRAMES SERVE THE SCRIPT: this stage binds one full-strip frame to each
segment, in order.

For each segment, in order:
  1. Aim at the panel orders the segment came from (`refers_to_orders`): the frame
     should come from at/near those orders in the strip. (No link -> just advance.)
  2. From the candidate panels near there, pick ONE frame that is NOT a pure
     text bubble. Using stage 2a's tags: PREFER has_face/has_person panels, then
     ALLOW impact/action/sound-effect panels (desirable), then any other panel
     that shows something. EXCLUDE only a PURE TEXT BUBBLE - `is_text_only` AND
     mostly white (no visible scene). A dark/busy `is_text_only` panel (a
     sound-effect splash with text drawn on the art) is NOT a pure bubble and is
     allowed.
  3. Reading-order MONOTONICITY: segment N's frame is at/after segment N-1's frame
     in the strip - never jump backward.
  4. If nothing good sits near the orders, WIDEN the search window; if still
     nothing, REUSE the previous frame rather than ever binding a text bubble.
  5. Use the FULL panel image (`Panel.crop`, no cropping) as the frame - not the
     tightened character crop.

Output: `Manifest.beats` - the ordered matched beats, one per segment, each a
frame-panel clone carrying that segment's narration in `script_line` and showing
the full panel. Downstream stages (s3 context, s6 tts, s7 video) consume these via
`Manifest.kept_panels()`. The matched candidates also get `selected=True`. For
review, `work/<chapter>/selected/` holds each matched frame named by segment order
plus a `narration.txt` pairing every segment to its frame (with the source panel
order + kind) so the match quality is easy to eyeball.

run(m) -> m, sets m.stage = "match" (between "script" and "context"); never saves.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from statistics import median

from manifest import Manifest, Panel

# A pure text bubble (to EXCLUDE) is text-only AND mostly white - no visible
# scene. Below this white fraction, a text-only panel is busy art (e.g. a
# sound-effect splash) and is allowed.
WHITE_BUBBLE_FRAC = 0.50

# Search windows (in candidate-order units) around a segment's refers_to_orders,
# tried widest-last. If none of them contain a forward frame, fall back to the
# nearest forward frame, then to reusing the previous one.
WINDOWS = (6, 12, 24)

# When a segment has NO refers_to_orders, advance through the strip but look this
# many frames ahead so we can still prefer a character frame over scenery.
LOOKAHEAD = 4


def _load_segments(work_dir: str) -> list[dict]:
    """Read STAGE A's segments.json (empty list if missing/empty/unreadable)."""
    path = Path(work_dir) / "script" / "segments.json"
    if not path.exists():
        print(f"  WARNING: no segments.json at {path}; run STAGE A (script) first")
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  WARNING: segments.json is unreadable ({e})")
        return []
    return data.get("segments", []) if isinstance(data, dict) else []


def _is_text_bubble(p: Panel) -> bool:
    """A PURE text bubble - the only thing matching excludes: a text-only panel
    that is mostly white (no visible scene). A dark/busy text-only panel (a
    sound-effect/action splash) is NOT a pure bubble."""
    if not p.is_text_only:
        return False
    wf = p.white_fraction
    return wf is None or wf >= WHITE_BUBBLE_FRAC


def _rank(p: Panel) -> int:
    """Preference among eligible frames, best (lowest) first: a character frame
    (face/person), then an impact/action panel, then anything else that shows
    something."""
    if p.has_face or p.has_person:
        return 0
    if p.frame_kind == "impact":
        return 1
    return 2


def _refers(seg: dict) -> list[int]:
    out: list[int] = []
    for o in seg.get("refers_to_orders") or []:
        try:
            out.append(int(o))
        except (TypeError, ValueError):
            continue
    return out


def _choose(eligible: list[Panel], last_j: int, refers: list[int]) -> int:
    """Index into `eligible` of the frame for this segment: nearest the segment's
    source orders, at/after the previous frame (monotonic), preferring stronger
    frame kinds; widen the window, then reuse, before ever going backward."""
    n = len(eligible)
    start = max(last_j, 0)
    if refers:
        lo, hi, center = min(refers), max(refers), median(refers)
        # 1) A frame whose order lies WITHIN the referenced span is on the very
        #    content the segment describes - take the best-ranked such frame
        #    (prefer character > impact > other). This keeps the frame on-topic
        #    instead of pulling in a stronger-kind frame from outside the span.
        on_span = [k for k in range(start, n) if lo <= eligible[k].order <= hi]
        if on_span:
            return min(on_span, key=lambda k: (_rank(eligible[k]),
                                               abs(eligible[k].order - center)))
        # 2) Nothing on the span: WIDEN around it. Now proximity leads (we're
        #    reaching outside the referenced panels), with rank as a tiebreak.
        for win in WINDOWS:
            cands = [k for k in range(start, n)
                     if lo - win <= eligible[k].order <= hi + win]
            if cands:
                return min(cands, key=lambda k: (abs(eligible[k].order - center),
                                                 _rank(eligible[k])))
        # 3) Nothing in any window: take the nearest forward frame.
        return min(range(start, n),
                   key=lambda k: (abs(eligible[k].order - center), _rank(eligible[k])))
    # No story link: advance through the strip, but prefer a character frame in a
    # small lookahead. At the end, this reuses the last frame.
    adv = last_j + 1 if last_j + 1 < n else last_j
    adv = max(adv, 0)
    cands = list(range(adv, min(n, adv + LOOKAHEAD + 1))) or [adv]
    return min(cands, key=lambda k: (_rank(eligible[k]), k))


def _make_beat(seq: int, text: str, src: Panel) -> Panel:
    """Clone the chosen frame into a standalone narration beat. Uses the FULL panel
    image (`src.crop`, crop_clean cleared) so the frame shows the whole panel, not
    the tightened character crop. Order = seq, so kept_panels() yields beats in
    segment order; a frame can back two beats with different narration."""
    return Panel(
        id=f"beat_{seq:03d}",
        bbox=list(src.bbox),
        order=seq,
        crop=src.crop,
        crop_clean=None,           # show the FULL panel, not the character crop
        speaker=src.speaker,
        dialogue=src.dialogue,
        has_person=src.has_person,
        has_face=src.has_face,
        frame_kind=src.frame_kind,
        selected=True,
        script_line=text,
    )


def _match(segments: list[dict],
           eligible: list[Panel]) -> tuple[list[Panel], list[Panel], int]:
    """Bind each segment to one eligible frame in order. Returns
    (beats, source_panels, reused_count)."""
    beats: list[Panel] = []
    sources: list[Panel] = []
    reused = 0
    last_j = -1
    for i, seg in enumerate(segments):
        text = str(seg.get("text") or "").strip()
        j = _choose(eligible, last_j, _refers(seg))
        src = eligible[j]
        if beats and j == last_j:
            reused += 1
        beats.append(_make_beat(i + 1, text, src))
        sources.append(src)
        src.selected = True            # flag the candidate for inspection
        src.script_line = text         # (last writer wins if reused)
        last_j = j
    return beats, sources, reused


def _src_kind(p: Panel) -> str:
    if p.has_face:
        return "FACE"
    if p.has_person:
        return "PERSON"
    if p.frame_kind == "impact":
        return "IMPACT"
    return "SCENE"


def _write_selected(selected_dir: Path, beats: list[Panel],
                    sources: list[Panel]) -> int:
    """Copy each matched FULL frame into selected/ named by segment order, and
    write narration.txt pairing every segment to its frame (source panel order +
    kind) for review. Returns the number of image files copied."""
    lines: list[str] = []
    written = 0
    for beat, src in zip(beats, sources):
        frame = beat.frame_image
        name = Path(frame).name if frame else "-"
        lines.append(f"[{beat.order:03d}] <- panel {src.order} ({name}, "
                     f"{_src_kind(src)}): {(beat.script_line or '').strip()}")
        if frame and Path(frame).exists():
            shutil.copy2(frame, selected_dir / f"sel_{beat.order:03d}_{name}")
            written += 1
    (selected_dir / "narration.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    return written


def run(m: Manifest) -> Manifest:
    selected_dir = Path(m.work_dir) / "selected"
    if selected_dir.exists():
        shutil.rmtree(selected_dir)
    selected_dir.mkdir(parents=True, exist_ok=True)

    segments = _load_segments(m.work_dir)
    panels = m.ordered_panels()
    for p in panels:                 # rebuilt fresh each run
        p.selected = False
    m.beats = []

    if not segments:
        print("  no script segments to match; no beats produced")
        m.stage = "match"
        return m

    # Eligible frames = every candidate that is NOT a pure text bubble, in reading
    # order. This keeps character, impact/action, sound-effect, and scenery panels;
    # it drops only mostly-white text-only bubbles. NEVER bind a pure bubble.
    n_bubble = sum(1 for p in panels if _is_text_bubble(p))
    eligible = sorted((p for p in panels if not _is_text_bubble(p)),
                      key=lambda p: p.order)
    if not eligible:
        print("  no non-bubble frames to match to; no beats produced")
        m.stage = "match"
        return m
    n_char = sum(1 for p in eligible if (p.has_face or p.has_person))
    n_impact = sum(1 for p in eligible if p.frame_kind == "impact")
    print(f"  eligible frames: {len(eligible)} of {len(panels)} candidates "
          f"({n_char} character, {n_impact} impact, "
          f"{len(eligible) - n_char - n_impact} other; {n_bubble} pure bubbles excluded)")

    beats, sources, reused = _match(segments, eligible)
    m.beats = beats

    written = _write_selected(selected_dir, beats, sources)
    distinct = len({s.order for s in sources})
    print(f"  matched {len(segments)} segments -> {len(beats)} beats "
          f"({distinct} distinct frames, {reused} reused)")
    print(f"  wrote {written} full-panel frames -> {selected_dir}")
    m.stage = "match"
    return m

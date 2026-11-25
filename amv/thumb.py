"""Thumbnails for AMV shorts: one clean, full-frame, vertical picture of a beautiful female character - no logo, no text.

`pick(items, work, rank)` asks Claude ONCE per chapter (cached in `work/shorts_bank/thumbs.json`) which of the bank's
clean face frames show the most beautiful female characters, best first; short number `rank` takes the `rank`-th of
them (wrapping), so the shorts of one chapter do not all share a thumbnail. A chapter with no suitable female frame
falls back to its strongest clean face. `make(frame, face_box, out)` cuts a 9:16 view around the face and writes a
1080x1920 JPEG. Nothing here touches the manifest or any stage; every failure degrades to "no thumbnail".
"""
from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SIZE = (1080, 1920)
CANDIDATES = 14

SCHEMA = {"type": "object", "required": ["ranked"], "properties": {"ranked": {
    "type": "array", "items": {"type": "string"}}}}
PROMPT = ("These are frames cut from a manhwa, each preceded by its id. Pick the frames that would make the best "
          "thumbnail for a vertical video. Rank them: FIRST the frames with a clearly visible FEMALE character who is "
          "beautiful or striking, then (after all of those) the most striking or handsome faces of anyone else. Only "
          "frames whose face is sharp and unobstructed, with NO speech bubbles, NO lettering and NO sound-effect "
          "text anywhere in the picture, and no panel borders or white margins. Return `ranked`: the ids of up to 6 "
          "frames, best first. Return an empty list when no frame qualifies.")


def _cache(work: Path) -> Path:
    return Path(work) / "shorts_bank" / "thumbs.json"


def _candidates(items: list[dict]) -> list[dict]:
    """Clean face frames of the bank (solo and fully visible ones first), the most striking first."""
    box = lambda t: (t["face_box"][2] - t["face_box"][0]) * (t["face_box"][3] - t["face_box"][1])  # noqa: E731
    clean = [x for x in items if (t := x["tag"]).get("has_face") and not t.get("text") and t.get("face_box")
             and t.get("visibility") in ("full", "partial") and Path(x["path"]).is_file()]
    clean.sort(key=lambda x: ((x["tag"].get("solo") and x["tag"].get("visibility") == "full"),
                              x["tag"].get("intensity", 0) + 12 * box(x["tag"])), reverse=True)
    return clean[:CANDIDATES]


def _jpeg(path: Path, side: int = 640) -> bytes:
    im = Image.open(path).convert("RGB")
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def ranked(items: list[dict], work: Path, model: str = "sonnet", log=print) -> list[str]:
    """Ids of the chapter's best thumbnail frames, best first (cached)."""
    cache = _cache(work)
    try:
        return json.loads(cache.read_text(encoding="utf-8"))["ranked"]
    except (OSError, ValueError, KeyError):
        pass
    cands = _candidates(items)
    if not cands:
        return []
    order = [x["id"] for x in cands]            # the fallback: strongest clean face first
    try:
        sys.path.insert(0, str(ROOT / "tools" / "framer"))
        sys.path.insert(0, str(ROOT))
        import claude_cli
        content = [{"type": "text", "text": PROMPT}]
        for x in cands:
            content += [{"type": "text", "text": f"{x['id']}:"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.b64encode(_jpeg(Path(x["path"]))).decode()}}]
        res = claude_cli.call(content, SCHEMA, model, ROOT, usage=claude_cli.empty_usage(), mention_event="read",
                              panel_ids=set(order), on_proc=lambda proc: None, on_event=lambda e: None,
                              on_limit=lambda when: (_ for _ in ()).throw(RuntimeError(f"usage limit until {when}")))
        picked = [i for i in res["ranked"] if i in order]
        order = picked or order                 # no female frame: the strongest clean face still beats nothing
    except Exception as e:  # noqa: BLE001 - a thumbnail is optional
        log(f"  thumbnail choice fell back to the strongest face ({type(e).__name__}: {e})")
    try:
        cache.write_text(json.dumps({"ranked": order}), encoding="utf-8")
    except OSError:
        pass
    return order


def _trim(im: Image.Image) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the picture without its white/black panel margins (loose: JPEG noise is common)."""
    import numpy as np
    g = np.asarray(im.convert("L"), dtype=np.float32)
    h, w = g.shape

    def blank(line) -> bool:
        m = float(line.mean())
        return float(line.std()) < 16 and (m > 225 or m < 30)

    top, bot, left, right = 0, h, 0, w
    ly, lx = int(h * .3), int(w * .3)
    while top < ly and blank(g[top]):
        top += 1
    while h - bot < ly and bot > top + 8 and blank(g[bot - 1]):
        bot -= 1
    while left < lx and blank(g[:, left]):
        left += 1
    while w - right < lx and right > left + 8 and blank(g[:, right - 1]):
        right -= 1
    iy, ix = int((bot - top) * .012) + 1, int((right - left) * .012) + 1      # a hair more, for the panel's own edge line
    return left + ix, top + iy, right - ix, bot - iy


def make(frame: Path, face_box: list[float] | None, out: Path) -> Path:
    """A 9:16 cut of `frame` (panel margins removed) around its face (the centre when there is no box) as a
    1080x1920 JPEG."""
    full = Image.open(frame).convert("RGB")
    l, t, r, b = _trim(full)
    im = full.crop((l, t, r, b))
    w, h = im.size
    target = SIZE[0] / SIZE[1]
    if face_box and face_box[2] > 0:
        fx = (((face_box[0] + face_box[2]) / 2) * full.width - l) / w
        fy = (((face_box[1] + face_box[3]) / 2) * full.height - t) / h
        fx, fy = min(max(fx, 0), 1), min(max(fy, 0), 1)
    else:
        fx, fy = .5, .5
    if w / h > target:                          # wider than 9:16: keep the full height, slide a window over the width
        cw, ch = h * target, h
    else:                                       # taller: keep the full width
        cw, ch = w, w / target
    left = min(max(fx * w - cw / 2, 0), w - cw)
    top = min(max(fy * h - ch * 0.4, 0), h - ch)   # the face sits in the upper part, like a poster
    im = im.crop((int(left), int(top), int(left + cw), int(top + ch))).resize(SIZE, Image.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "JPEG", quality=92)
    return out


def pick(items: list[dict], work: Path, rank: int, out: Path, log=print) -> Path | None:
    """The thumbnail for the `rank`-th short of the chapter, or None when the bank has no usable face frame."""
    order = ranked(items, work, log=log)
    if not order:
        return None
    by_id = {x["id"]: x for x in items}
    x = by_id.get(order[rank % len(order)])
    if x is None:
        return None
    try:
        return make(Path(x["path"]), x["tag"].get("face_box"), out)
    except OSError as e:
        log(f"  thumbnail failed: {e}")
        return None

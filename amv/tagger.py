"""Frame tagging for AMV shorts: where is the face, where do the eyes sit, who is it, how intense.

One Claude call per ~16 images (the app's own `claude` CLI wrapper, so it uses the same login
and usage accounting as the script engine). The answer is cached as JSON beside the frames'
hash, so a frame is only ever read once. Coordinates are fractions of the ORIGINAL image
[x, y]; the renderer converts them after it trims panel borders.

    {"<path>": {"has_face": bool, "face_box": [l,t,r,b], "eye": [x,y], "character": str,
                "kind": "face"|"action"|"scenery"|"aftermath"|"object",
                "expression": str, "intensity": 1-10, "text": bool}}
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache" / "amv_tags"
BATCH = 16
TAG_VERSION = 2          # bump when the schema changes: older cached tags are read again

POINT = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}

SCHEMA = {
    "type": "object",
    "properties": {"frames": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "has_face": {"type": "boolean"},
            "face_box": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
            "eye": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
            "character": {"type": "string"},
            "kind": {"type": "string", "enum": ["face", "action", "scenery", "aftermath", "object"]},
            "expression": {"type": "string"},
            "intensity": {"type": "integer"},
            "text": {"type": "boolean"},
            "visibility": {"type": "string", "enum": ["full", "partial", "hidden"]},
            "solo": {"type": "boolean"},
            "mouth": POINT, "hand": POINT, "weapon": POINT,
        },
        "required": ["id", "has_face", "face_box", "eye", "character", "kind", "expression", "intensity", "text",
                     "visibility", "solo", "mouth", "hand", "weapon"],
    }}},
    "required": ["frames"],
}

PROMPT = (
    "These are frames cut from a manhwa, each preceded by its id. For EVERY frame return: `has_face` "
    "(a clearly visible face of the main figure, even partly turned; false for backs of heads, hands, "
    "weapons, crowds, scenery); `face_box` as fractions of the frame [left, top, right, bottom] tight "
    "around that face (all zeros when no face); `eye` as [x, y] fractions: the point between the eyes of "
    "that face (for a profile the visible eye; for no face the visual centre of interest); `character` "
    "the character's name if the context below tells it, else a short visual description (hair, clothes); "
    "`kind`: face (a face is the subject), action (fighting, motion, effects), scenery, aftermath "
    "(results, damage, bodies, quiet after) or object (a hand, weapon, item); `expression` in 1-3 words; "
    "`intensity` 1-10 for how striking and emotionally strong the frame is; `text` true when readable "
    "speech bubbles or lettering sit in the picture; `visibility` of the main figure's face: full, partial "
    "(turned, shadowed, half cropped, hair or a mask over part of it) or hidden (back, silhouette, no face); "
    "`solo` true when exactly one character is in the frame; `mouth`, `hand` (the most prominent hand) and "
    "`weapon` as [x, y] fractions of the frame, [0, 0] when that part is not visible.\n\n{context}"
)


def _jpeg(path: Path, side: int = 768) -> bytes:
    im = Image.open(path).convert("RGB")
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _key(path: Path) -> str:
    st = path.stat()
    return hashlib.sha1(f"v{TAG_VERSION}|{path.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:20]


def tag_frames(paths: list[str | Path], context: str = "", model: str = "sonnet",
               log=print) -> dict[str, dict]:
    """Tags for every path (cached ones are not sent again). Returns {str(path): tag}."""
    CACHE.mkdir(parents=True, exist_ok=True)
    paths = [Path(p) for p in paths]
    out: dict[str, dict] = {}
    todo: list[Path] = []
    for p in paths:
        f = CACHE / f"{_key(p)}.json"
        if f.exists():
            try:
                out[str(p)] = json.loads(f.read_text(encoding="utf-8"))
                continue
            except (OSError, ValueError):
                pass
        todo.append(p)
    if not todo:
        return out

    sys.path.insert(0, str(ROOT / "tools" / "framer"))
    sys.path.insert(0, str(ROOT))
    import claude_cli  # the app's wrapper around the local `claude` CLI

    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        ids = {f"F{j + 1:02d}": p for j, p in enumerate(chunk)}
        content = [{"type": "text", "text": PROMPT.format(context=context or "(no extra context)")}]
        for fid, p in ids.items():
            content += [{"type": "text", "text": f"{fid}:"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.b64encode(_jpeg(p)).decode()}}]
        usage = claude_cli.empty_usage()
        log(f"tagging {len(chunk)} frames ({i + len(chunk)}/{len(todo)})")
        res = claude_cli.call(content, SCHEMA, model, ROOT, usage=usage, mention_event="read",
                              panel_ids=set(ids), on_proc=lambda proc: None, on_event=lambda e: None,
                              on_limit=lambda when: (_ for _ in ()).throw(RuntimeError(f"usage limit until {when}")))
        for entry in res["frames"]:
            p = ids.get(entry["id"])
            if p is None:
                continue
            tag = {k: v for k, v in entry.items() if k != "id"}
            out[str(p)] = tag
            (CACHE / f"{_key(p)}.json").write_text(json.dumps(tag), encoding="utf-8")
    return out

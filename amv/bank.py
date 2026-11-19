"""The shorts frame bank: a chapter's tagged, ready-to-use frames, kept beside the chapter.

`build(work)` reads the chapter's exported frames (`work/frames`, listed by `work/framer/mapping.json`),
tags them (amv.tagger), keeps the best ones and copies those into `work/shorts_bank/frames/`, then writes
`work/shorts_bank/bank.json`. After that the shorts need nothing else of the chapter: the big `frames`
folder can be deleted and restored from Drive without the bank noticing, and the bank has its own backup
kinds (tools/framer/chapter_backup.py: `bank.json` rides with the script, the frames folder is `shorts_frames`).

    bank.json = {"version": 1, "chapter_id": str, "built": iso time,
                 "frames": [{"id", "file", "order", "line", "tag": {...tagger tag...}, "keep": bool}]}

`file` is relative to `work/shorts_bank/frames/` and exists only when `keep` is true; tags are stored for
every frame so a later plan can still see what the dropped ones were. Frames are only read, never changed.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import tagger

VERSION = 1
MAX_KEEP = 60            # frames copied into the bank per chapter
MIN_INTENSITY = 4        # a frame weaker than this is only kept when it shows a face


def bank_dir(work: Path) -> Path:
    return Path(work) / "shorts_bank"


def bank_path(work: Path) -> Path:
    return bank_dir(work) / "bank.json"


def frames_dir(work: Path) -> Path:
    return bank_dir(work) / "frames"


def exists(work: Path) -> bool:
    return bank_path(work).is_file()


def _read(work: Path) -> dict | None:
    try:
        data = json.loads(bank_path(work).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("frames"), list) else None


def source_frames(work: Path, frames: Path | None = None) -> list[dict]:
    """The chapter's exported frames in reading order: {"id", "path", "order", "line"}. `frames` is the folder
    to read them from when they are not in `work/frames` (e.g. freshly cut from the mapping)."""
    work = Path(work)
    frames = Path(frames) if frames else work / "frames"
    try:
        mapping = json.loads((work / "framer" / "mapping.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[dict] = []
    for entry in mapping.get("lines", []):
        for fr in entry.get("frames", []):
            name = fr.get("file")
            if name:
                out.append({"id": Path(name).stem, "path": frames / name,
                            "order": entry.get("order", len(out) + 1), "line": entry.get("text", "")})
    return out


def _score(tag: dict) -> float:
    s = float(tag.get("intensity", 0))
    if tag.get("has_face"):
        s += 2
    if tag.get("solo"):
        s += 1
    return s


def choose(entries: list[dict], max_keep: int = MAX_KEEP) -> set[str]:
    """Ids of the frames worth keeping: no readable text, strong or showing a face, best `max_keep`."""
    ok = [e for e in entries
          if not e["tag"].get("text")
          and (e["tag"].get("intensity", 0) >= MIN_INTENSITY or e["tag"].get("has_face"))]
    ok.sort(key=lambda e: _score(e["tag"]), reverse=True)
    return {e["id"] for e in ok[:max_keep]}


def build(work: Path, context: str = "", model: str = "sonnet", max_keep: int = MAX_KEEP,
          rebuild: bool = False, frames: Path | None = None, log=print) -> dict:
    """Make (or, when `rebuild`, remake) the chapter's bank and return it. A chapter that already has one
    is left alone, so this is safe to run over every chapter."""
    work = Path(work)
    if not rebuild:
        had = _read(work)
        if had is not None and had.get("version") == VERSION:
            return had
    src = [s for s in source_frames(work, frames) if s["path"].is_file()]
    if not src:
        raise FileNotFoundError(f"No exported frames under {frames or work / 'frames'}")
    tags = tagger.tag_frames([s["path"] for s in src], context=context, model=model, log=log)
    entries = []
    for s in src:
        tag = tags.get(str(s["path"]))
        if tag is None:
            continue
        entries.append({"id": s["id"], "order": s["order"], "line": s["line"], "tag": tag, "_src": s["path"]})
    keep = choose(entries, max_keep)

    out = frames_dir(work)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    frames = []
    for e in entries:
        item = {"id": e["id"], "order": e["order"], "line": e["line"], "tag": e["tag"], "keep": e["id"] in keep}
        if item["keep"]:
            item["file"] = e["_src"].name
            shutil.copy2(e["_src"], out / item["file"])
        frames.append(item)
    data = {"version": VERSION, "chapter_id": work.parent.name, "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "frames": frames}
    tmp = bank_path(work).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(bank_path(work))
    log(f"bank: kept {len(keep)} of {len(frames)} frames")
    return data


def load(work: Path) -> list[dict]:
    """The kept frames as {"path", "tag", "ch", "id", "order", "line"} for the planner (a missing file is skipped)."""
    data = _read(work)
    if data is None:
        return []
    out = []
    for f in data["frames"]:
        p = frames_dir(work) / f["file"] if f.get("keep") and f.get("file") else None
        if p is not None and p.is_file():
            out.append({"path": str(p), "tag": f["tag"], "ch": data.get("chapter_id", ""), "id": f["id"],
                        "order": f["order"], "line": f["line"]})
    return out


def summary(work: Path) -> dict:
    data = _read(work)
    if data is None:
        return {"built": False, "frames": 0, "kept": 0}
    return {"built": True, "frames": len(data["frames"]), "kept": sum(1 for f in data["frames"] if f.get("keep")),
            "at": data.get("built")}

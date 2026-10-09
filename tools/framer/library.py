"""Per-project media library - no Flask here.

Lists what a project keeps on disk, grouped per chapter into file types, and
deletes whole types. Only REGENERABLE types are known to this module: anything
else in a chapter (script, frame mapping, editor state, manifest, settings,
custom voice/music/watermark uploads) is never matched by a type, so it cannot
be listed for deletion or deleted from here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import projects
from atomic_io import read_json_with_backup_fallback
from manifest import Manifest


@dataclass(frozen=True)
class Kind:
    key: str
    label: str
    regenerate: str
    collect: Callable[[Path], list[Path]]


def _files(directory: Path, pattern: str = "*") -> list[Path]:
    return sorted(p for p in directory.glob(pattern) if p.is_file()) if directory.is_dir() else []


def _video(chapter: Path) -> list[Path]:
    out = chapter / "output"
    return [p for p in (out / "recap.mp4", out / "video_plan.json") if p.is_file()]


def _strips(chapter: Path) -> list[Path]:
    framer = chapter / "work" / "framer"
    return _files(chapter / "work" / "pages") + _files(framer / "tiles") + \
        [p for p in (framer / "preview.json",) if p.is_file()]


def _pdfs(chapter: Path) -> list[Path]:
    work = chapter / "work"
    downloads = sorted(p for p in (work / "download").rglob("*.pdf")) if (work / "download").is_dir() else []
    return downloads + _files(work / "gemini_parts", "*.pdf")


KINDS: dict[str, Kind] = {k.key: k for k in (
    Kind("video", "Rendered video",
         "Re-render video (fast) from the kept audio, frames and mapping.",
         _video),
    Kind("audio", "Narration audio",
         "Generate audio + video again; the narration is synthesized from the script.",
         lambda c: _files(c / "work" / "audio")),
    Kind("frames", "Exported frames",
         "Export again in the editor: frames are re-cut from the stitched strip with "
         "your boxes (the strip must exist). Export resets the narration, so run "
         "Generate audio + video afterwards.",
         lambda c: _files(c / "work" / "frames")),
    Kind("strips", "Stitched strip + preview tiles",
         "Re-stitch from the PDF in the editor (Chapter source). Frames cannot be "
         "re-exported until the strip exists again.",
         _strips),
    Kind("pdfs", "Downloaded PDF + Gemini parts",
         "Re-download the chapter in the editor. Re-stitching the strip needs the PDF, "
         "and the Gemini parts are split from it again.",
         _pdfs),
)}


def _size(path: Path) -> int:
    return path.stat().st_size


def _tree_size(root: Path) -> int:
    total = 0
    for folder, _, names in os.walk(root):
        for name in names:
            try:
                total += (Path(folder) / name).stat().st_size
            except OSError:
                continue
    return total


def kind_files(slug: str, n, key: str) -> list[Path]:
    return KINDS[key].collect(projects.chapter_dir(slug, n))


def chapter_summary(slug: str, n) -> dict:
    chapter = projects.chapter_dir(slug, n)
    kinds = {}
    for key, kind in KINDS.items():
        files = kind.collect(chapter)
        kinds[key] = {"count": len(files), "bytes": sum(_size(p) for p in files)}
    total = _tree_size(chapter) if chapter.is_dir() else 0
    return {"kinds": kinds, "bytes": total,
            "kept_bytes": total - sum(k["bytes"] for k in kinds.values())}


def overview(slug: str, merged_bytes: int) -> dict:
    """Space used by the project, per chapter and per type."""
    project = projects.load(slug)
    chapters, by_kind = [], {key: 0 for key in KINDS}
    for ch in project["chapters"]:
        summary = chapter_summary(slug, ch["n"])
        if not summary["bytes"]:
            continue
        for key, info in summary["kinds"].items():
            by_kind[key] += info["bytes"]
        chapters.append({"n": ch["n"], "title": ch["title"], "status": ch["status"], **summary})
    chapter_bytes = sum(c["bytes"] for c in chapters)
    total = _tree_size(projects.project_dir(slug))
    return {
        "kinds": [{"key": k.key, "label": k.label, "regenerate": k.regenerate} for k in KINDS.values()],
        "total_bytes": total, "by_kind": by_kind, "merged_bytes": merged_bytes,
        "kept_bytes": total - merged_bytes - sum(by_kind.values()),
        "chapters": chapters,
    }


def chapter_detail(slug: str, n) -> dict:
    """Every file of a chapter, grouped by type, plus the work that is kept."""
    chapter = projects.chapter_dir(slug, n)
    groups, listed = [], set()
    for key, kind in KINDS.items():
        files = kind.collect(chapter)
        listed.update(files)
        groups.append({"key": key, "label": kind.label, "regenerate": kind.regenerate,
                       "bytes": sum(_size(p) for p in files),
                       "files": [{"name": p.name, "path": p.relative_to(chapter).as_posix(),
                                  "size": _size(p)} for p in files]})
    kept = [{"name": p.relative_to(chapter).as_posix(), "size": _size(p)}
            for p in sorted(chapter.rglob("*")) if p.is_file() and p not in listed]
    return {"groups": groups, "kept": kept}


def servable_file(slug: str, n, rel: str) -> Path | None:
    """`rel` as a path inside the chapter, only if some type owns that file."""
    chapter = projects.chapter_dir(slug, n)
    target = chapter / rel
    for key in KINDS:
        if target in kind_files(slug, n, key):
            return target
    return None


def plan_delete(slug: str, chapters: list, key: str) -> dict:
    """What deleting type `key` across `chapters` would remove."""
    per_chapter = {}
    for n in chapters:
        files = kind_files(slug, n, key)
        per_chapter[str(n)] = {"count": len(files), "bytes": sum(_size(p) for p in files)}
    return {"bytes": sum(c["bytes"] for c in per_chapter.values()),
            "count": sum(c["count"] for c in per_chapter.values()),
            "chapters": per_chapter}


def delete_kind(slug: str, chapters: list, key: str) -> tuple[int, int]:
    """Delete type `key` from `chapters`. Returns (bytes freed, files left because
    something - typically a player streaming them - still has them open)."""
    freed = locked = 0
    for n in chapters:
        for path in kind_files(slug, n, key):
            size = _size(path)
            try:
                path.unlink()
            except PermissionError:
                locked += 1
            else:
                freed += size
    return freed, locked


def _mapped_frames(work: Path) -> list[str]:
    try:
        mapping = read_json_with_backup_fallback(work / "framer" / "mapping.json")
    except Exception:  # noqa: BLE001 - no usable mapping: callers fall back to the manifest
        return []
    return [f["image"] for line in mapping.get("lines", []) for f in line.get("frames", [])]


def render_blockers(work: Path, what: str) -> list[str]:
    """Reasons a `what` render (both|voice|video) can't run because files it
    needs were deleted, each saying how to get them back."""
    if what == "voice":
        return []
    try:
        beats = Manifest.load(work).beats
    except Exception:  # noqa: BLE001 - a missing manifest is reported by the caller
        return []
    blockers = []
    frames = _mapped_frames(work) or [b.crop for b in beats]
    if any(not f or not Path(f).is_file() for f in frames):
        blockers.append("Exported frames are missing. " + KINDS["frames"].regenerate)
    if what == "video" and any(b.audio and not Path(b.audio).is_file() for b in beats):
        blockers.append("Narration audio is missing. " + KINDS["audio"].regenerate)
    return blockers

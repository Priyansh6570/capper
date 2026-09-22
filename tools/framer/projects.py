"""Project storage for the framer's project system - no Flask here.

A "project" is one manhwa/manhua/manga series. Its folder holds `project.json`
(name, source URL, chapter list + status, media settings) plus per-chapter work
and output directories:

    projects/<slug>/
      project.json
      assets/                       uploaded music / voice ref / watermark / background
      chapters/<n>/
        work/                       download/ pages/ framer/ frames/ audio/ ...
        output/                     recap.mp4, video_plan.json

`config.chapter_work_dir()`/`chapter_output_dir()` are redirected to a
project's `chapters/<n>/{work,output}` via `config.chapter_scope()` - see
config.py. Nothing here imports Flask; app.py wires this to routes.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import PROJECTS_DIR

# Status rank - forward-only transitions (never downgraded automatically).
STATUS_RANK = {"not_started": 0, "downloaded": 1, "boxed": 2, "rendered": 3, "complete": 4}
SCHEMA_VERSION = 1

_LOCK = threading.Lock()  # guards read-modify-write of any project.json


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "project"


def unique_slug(name: str) -> str:
    base = slugify(name)
    slug = base
    i = 2
    while (PROJECTS_DIR / slug).exists():
        slug = f"{base}-{i}"
        i += 1
    return slug


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def project_dir(slug: str) -> Path:
    return PROJECTS_DIR / slug


def project_json_path(slug: str) -> Path:
    return project_dir(slug) / "project.json"


def assets_dir(slug: str) -> Path:
    d = project_dir(slug) / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def chapter_dir(slug: str, n) -> Path:
    return project_dir(slug) / "chapters" / str(n)


def chapter_work_dir(slug: str, n) -> Path:
    d = chapter_dir(slug, n) / "work"
    d.mkdir(parents=True, exist_ok=True)
    return d


def chapter_output_dir(slug: str, n) -> Path:
    d = chapter_dir(slug, n) / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


def chapter_video_path(slug: str, n) -> Path:
    return chapter_output_dir(slug, n) / "recap.mp4"


def merged_dir(slug: str) -> Path:
    d = project_dir(slug) / "merged"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# schema helpers (dataclasses used only for shape/defaults; disk format is dict)
# --------------------------------------------------------------------------- #
def _default_settings() -> dict:
    return {
        "music": {"mode": "default", "file": None},
        "voice": {"mode": "default", "file": None},
        "watermark": {"enabled": False, "type": "text", "text": "", "file": None,
                      "opacity": 0.12, "size": 0.12, "spacing": 1.0, "angle": -30},
        "background": {"mode": "blur", "file": None, "dim": 0.85},
    }


def _default_chapter(n: int, title: str = "") -> dict:
    return {"n": n, "title": title or f"Episode {n}", "status": "not_started",
             "pdf": None, "video": None, "updated_at": None}


def new_project(name: str, url: str, slug: Optional[str] = None) -> dict:
    slug = slug or unique_slug(name)
    now = _now_iso()
    return {
        "schema": SCHEMA_VERSION, "slug": slug, "name": name.strip() or slug,
        "url": (url or "").strip(), "series_title": None, "source": "manual",
        "created_at": now, "updated_at": now,
        "chapters": [], "settings": _default_settings(),
    }


# --------------------------------------------------------------------------- #
# load / save / list
# --------------------------------------------------------------------------- #
def exists(slug: str) -> bool:
    return project_json_path(slug).is_file()


def load(slug: str) -> dict:
    data = json.loads(project_json_path(slug).read_text(encoding="utf-8"))
    data.setdefault("schema", SCHEMA_VERSION)
    data.setdefault("chapters", [])
    data.setdefault("merges", [])
    data.setdefault("settings", _default_settings())
    settings = _default_settings()
    _deep_merge(settings, data["settings"])
    data["settings"] = settings
    for ch in data["chapters"]:
        ch.setdefault("title", f"Episode {ch.get('n')}")
        ch.setdefault("status", "not_started")
        ch.setdefault("pdf", None)
        ch.setdefault("video", None)
        ch.setdefault("updated_at", None)
    return data


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def save(project: dict) -> None:
    project["updated_at"] = _now_iso()
    _atomic_write(project_json_path(project["slug"]), json.dumps(project, indent=2, ensure_ascii=False))


def create(name: str, url: str) -> dict:
    project = new_project(name, url)
    project_dir(project["slug"]).mkdir(parents=True, exist_ok=True)
    assets_dir(project["slug"])
    save(project)
    return project


def delete(slug: str) -> None:
    import shutil
    d = project_dir(slug)
    d.resolve().relative_to(PROJECTS_DIR.resolve())  # refuse anything outside PROJECTS_DIR
    shutil.rmtree(d, ignore_errors=True)


def list_projects() -> list[dict]:
    out = []
    if not PROJECTS_DIR.is_dir():
        return out
    for d in sorted(PROJECTS_DIR.iterdir()):
        if not (d / "project.json").is_file():
            continue
        try:
            p = load(d.name)
        except Exception:  # noqa: BLE001 - a corrupt project.json must not sink the list
            continue
        counts: dict[str, int] = {}
        for ch in p["chapters"]:
            counts[ch["status"]] = counts.get(ch["status"], 0) + 1
        out.append({
            "slug": p["slug"], "name": p["name"], "url": p["url"],
            "chapters": len(p["chapters"]), "counts": counts,
            "updated_at": p.get("updated_at"),
        })
    out.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return out


# --------------------------------------------------------------------------- #
# chapters
# --------------------------------------------------------------------------- #
def fill_chapters(project: dict, chapters: list[dict]) -> dict:
    """Replace the chapter list, preserving status/pdf/video for numbers that
    already existed (a re-fetch or a manual range must not reset progress)."""
    existing = {ch["n"]: ch for ch in project.get("chapters", [])}
    out = []
    for c in chapters:
        n = c["n"]
        if n in existing:
            merged = dict(existing[n])
            if c.get("title"):
                merged["title"] = c["title"]
            out.append(merged)
        else:
            out.append(_default_chapter(n, c.get("title", "")))
    project["chapters"] = sorted(out, key=lambda c: c["n"])
    return project


def manual_range(project: dict, start: int, end: int) -> dict:
    chapters = [{"n": n, "title": f"Episode {n}"} for n in range(start, end + 1)]
    return fill_chapters(project, chapters)


def get_chapter(project: dict, n) -> Optional[dict]:
    n = int(n)
    for ch in project["chapters"]:
        if ch["n"] == n:
            return ch
    return None


def set_status(slug: str, n, status: str, *, force: bool = False) -> dict:
    """Read-modify-write a chapter's status. Forward-only unless force=True
    (used by the manual "Mark complete" / "reset" actions)."""
    if status not in STATUS_RANK:
        raise ValueError(f"unknown status: {status}")
    with _LOCK:
        project = load(slug)
        ch = get_chapter(project, n)
        if ch is None:
            raise KeyError(f"no chapter {n} in project {slug}")
        if force or STATUS_RANK[status] > STATUS_RANK.get(ch["status"], 0):
            ch["status"] = status
            ch["updated_at"] = _now_iso()
            save(project)
        return ch


def derive_status(slug: str, n) -> dict:
    """Re-derive a chapter's status from what's actually on disk (pdf ->
    downloaded, mapping.json -> boxed, recap.mp4 -> rendered) and merge it with
    the stored status by rank, so an interrupted write never lies. Never
    downgrades a manually-set "complete"."""
    with _LOCK:
        project = load(slug)
        ch = get_chapter(project, n)
        if ch is None:
            raise KeyError(f"no chapter {n} in project {slug}")
        work = chapter_work_dir(slug, n)
        out = chapter_output_dir(slug, n)
        derived = "not_started"
        if any((work / "download").glob("*.pdf")) if (work / "download").is_dir() else False:
            derived = "downloaded"
        if (work / "framer" / "mapping.json").is_file():
            derived = "boxed"
        if (out / "recap.mp4").is_file():
            derived = "rendered"
        if STATUS_RANK[derived] > STATUS_RANK.get(ch["status"], 0):
            ch["status"] = derived
            ch["updated_at"] = _now_iso()
            save(project)
        return ch


# --------------------------------------------------------------------------- #
# settings + media snapshot for stage 7 (see stages/s7_assemble.py)
# --------------------------------------------------------------------------- #
VALID_ASSET_KINDS = ("music", "voice", "watermark", "background")
_ASSET_EXTS = {
    "music": {".mp3", ".wav", ".m4a", ".ogg", ".oga", ".aac", ".flac"},
    "voice": {".wav"},          # Chatterbox reference clip - wav only
    "watermark": {".png", ".jpg", ".jpeg", ".webp"},
    "background": {".png", ".jpg", ".jpeg", ".webp"},
}


def update_settings(slug: str, settings: dict) -> dict:
    with _LOCK:
        project = load(slug)
        _deep_merge(project["settings"], settings)
        # clamp numeric sliders into sane ranges regardless of what the client sent
        wm = project["settings"]["watermark"]
        wm["opacity"] = max(0.0, min(1.0, float(wm.get("opacity", 0.12))))
        wm["size"] = max(0.02, min(0.6, float(wm.get("size", 0.12))))
        wm["spacing"] = max(0.0, min(4.0, float(wm.get("spacing", 1.0))))
        wm["angle"] = max(-90, min(90, float(wm.get("angle", -30))))
        bg = project["settings"]["background"]
        bg["dim"] = max(0.1, min(1.0, float(bg.get("dim", 0.85))))
        save(project)
    write_all_media_snapshots(slug)
    return project


def save_asset(slug: str, kind: str, filename: str, file_storage) -> str:
    """Save an uploaded file into projects/<slug>/assets/, returning the path
    stored in project.json (relative to the project folder)."""
    if kind not in VALID_ASSET_KINDS:
        raise ValueError(f"unknown asset kind: {kind}")
    ext = Path(filename or "").suffix.lower()
    if ext not in _ASSET_EXTS[kind]:
        raise ValueError(f"unsupported file type for {kind}: {ext or '(none)'}")
    safe_name = f"{kind}{ext}"
    dest = assets_dir(slug) / safe_name
    file_storage.save(str(dest))
    return f"assets/{safe_name}"


def find_asset(slug: str, kind: str) -> Optional[Path]:
    """The saved asset file for `kind` (music/watermark/background), regardless
    of its extension - used by the settings live-preview routes."""
    matches = sorted(assets_dir(slug).glob(f"{kind}.*"))
    return matches[0] if matches else None


def resolve_asset_path(slug: str, rel_path: Optional[str]) -> Optional[Path]:
    if not rel_path:
        return None
    p = (project_dir(slug) / rel_path).resolve()
    try:
        p.relative_to(project_dir(slug).resolve())
    except ValueError:
        return None
    return p if p.is_file() else None


def _resolved_str(slug: str, rel_path: Optional[str]) -> Optional[str]:
    """str(resolve_asset_path(...)) but WITHOUT turning a missing file into the
    literal string "None" (str(None) == "None", a truthy non-empty string
    that downstream `if cfg.get("file")` checks in s7/s6 would treat as "yes,
    there's a file" - silently defeating the very check meant to skip a
    missing/deleted asset)."""
    p = resolve_asset_path(slug, rel_path)
    return str(p) if p is not None else None


def media_settings_snapshot(slug: str) -> dict:
    """Resolve a project's settings into ABSOLUTE paths for stages 6+7 to
    consume, with defaults folded in (None fields mean "use the built-in
    default"). `voice` is read by stage 6 (s6_tts) for Chatterbox cloning;
    `music`/`watermark`/`background` are read by stage 7 (s7_assemble)."""
    project = load(slug)
    s = project["settings"]
    music = s["music"]
    voice = s["voice"]
    watermark = dict(s["watermark"])
    background = s["background"]
    out = {
        "music": _resolved_str(slug, music.get("file")) if music.get("mode") == "file" else None,
        "voice": _resolved_str(slug, voice.get("file")) if voice.get("mode") == "file" else None,
        "watermark": {
            **watermark,
            "file": _resolved_str(slug, watermark.get("file")) if watermark.get("type") == "image" else None,
        } if watermark.get("enabled") else None,
        "background": {
            "file": _resolved_str(slug, background.get("file")),
            "dim": background.get("dim", 0.85),
        } if background.get("mode") == "image" else None,
    }
    return out


def write_media_snapshot(slug: str, n) -> Optional[Path]:
    """Write the resolved media_settings.json into one chapter's work dir, if
    that chapter has ever been worked on (its work dir already exists)."""
    work = chapter_dir(slug, n) / "work"
    if not work.is_dir():
        return None
    path = work / "media_settings.json"
    _atomic_write(path, json.dumps(media_settings_snapshot(slug), indent=2, ensure_ascii=False))
    return path


def write_all_media_snapshots(slug: str) -> list[Path]:
    project = load(slug)
    written = []
    for ch in project["chapters"]:
        p = write_media_snapshot(slug, ch["n"])
        if p:
            written.append(p)
    return written


# --------------------------------------------------------------------------- #
# merged videos (multiple chapters' recap.mp4 concatenated into one) - see
# app.py's /api/projects/<slug>/merge_stream. Purely a framer-side feature:
# it only reads already-rendered recap.mp4 files, so it needs no changes to
# the manifest, orchestrator, or any pipeline stage.
# --------------------------------------------------------------------------- #
def record_merge(slug: str, entry: dict) -> dict:
    """Append one completed merge's record ({file, chapters, mode,
    created_at}) to project.json, most-recent first."""
    with _LOCK:
        project = load(slug)
        project.setdefault("merges", []).insert(0, entry)
        save(project)
        return project

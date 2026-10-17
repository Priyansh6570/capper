"""A project's cover image and description, taken from its source site.

`save(project, fetched)` is called wherever a series is scraped (project creation, Refetch). The
cover is downloaded once to `<project>/cover.jpg` (small JPEG), so the Projects screen never
depends on the site's image server, which may refuse hotlinking. Best-effort: any failure leaves
the project as it was.
"""
from __future__ import annotations

import io
from pathlib import Path

import requests
from PIL import Image

import projects

COVER_NAME = "cover.jpg"
COVER_HEIGHT = 480
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def cover_path(slug: str) -> Path:
    return projects.project_dir(slug) / COVER_NAME


def _download_cover(url: str, referer: str, dest: Path) -> bool:
    try:
        resp = requests.get(url, headers={**_HEADERS, "Referer": referer}, timeout=(5, 15))
        if resp.status_code != 200 or not resp.content:
            return False
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        img.thumbnail((COVER_HEIGHT * 2, COVER_HEIGHT))
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, "JPEG", quality=88)
        return True
    except Exception:  # noqa: BLE001 - a missing cover must never fail project creation
        return False


def save(project: dict, fetched: dict) -> None:
    """Store the scraped description on `project` (the caller saves it) and fetch its cover."""
    if fetched.get("description"):
        project["description"] = fetched["description"]
    if fetched.get("cover_url"):
        _download_cover(fetched["cover_url"], fetched.get("list_url") or project.get("url") or "",
                        cover_path(project["slug"]))

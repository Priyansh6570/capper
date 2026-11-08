"""roliascan.com adapter: series-metadata scraping + chapter image download over plain HTTP.

The series page (`/manga/<slug>/`) carries the title, cover and a numeric manga id; the chapter list
is loaded by script from `/auth/manga-chapters?manga_id=<id>` (JSON, each entry has the chapter's
number and its `/read/<slug>/ch<n>-<chapter id>/` URL - the id means a chapter URL can't be built from
its number alone). A chapter's images come from `/auth/chapter-content?chapter_id=<id>`. Downloaded
images are assembled into a single PDF so everything after Stitch PDF runs the same code path as the
other sources. A chapter uploaded more than once (several groups) uses its first listed upload.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

import requests
from bs4 import BeautifulSoup

from . import meta
from .images import fetch_pages, images_to_pdf

SITE_NAME = "roliascan.com"
HOME_URL = "https://roliascan.com/"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}
_TIMEOUT = (5, 30)  # (connect, read) seconds - the image host is slow now and then
_PAGE_SIZE = 500

_SLUG_RE = re.compile(r"roliascan\.com/(?:manga|read)/([a-z0-9-]+)", re.I)
_CHAPTER_URL_RE = re.compile(r"/read/[a-z0-9-]+/ch([0-9-]+)-(\d+)/?", re.I)
_MANGA_ID_RE = re.compile(r"manga[_-]?id[\"'=: ]+[\"']?(\d+)", re.I)


def matches(url: str) -> bool:
    return "roliascan.com" in (url or "").lower()


def _slug(url: str) -> Optional[str]:
    m = _SLUG_RE.search(url or "")
    return m.group(1) if m else None


def _get(url: str, headers: Optional[dict] = None, **kwargs) -> requests.Response:
    resp = requests.get(url, headers=headers or _HEADERS, timeout=_TIMEOUT, **kwargs)
    if resp.status_code != 200 or not resp.text:
        raise RuntimeError(f"Couldn't load {url} (HTTP {resp.status_code}).")
    return resp


def _series_page(slug: str) -> tuple[BeautifulSoup, str]:
    """The parsed series page and its manga id."""
    html = _get(f"{HOME_URL}manga/{slug}/").text
    found = _MANGA_ID_RE.search(html)
    if not found:
        raise RuntimeError(f"Couldn't find the series id on roliascan.com/manga/{slug}/.")
    return BeautifulSoup(html, "html.parser"), found.group(1)


def _chapter_list(manga_id: str) -> list[dict]:
    """Every chapter the site lists for the series (raw API entries), oldest first."""
    entries: list[dict] = []
    while True:
        data = _get(f"{HOME_URL}auth/manga-chapters",
                    params={"manga_id": manga_id, "offset": len(entries), "limit": _PAGE_SIZE,
                            "order": "ASC"}).json()
        batch = data.get("chapters") or []
        entries += batch
        if not data.get("success") or not data.get("has_more") or not batch:
            return entries


def _whole_number(entry: dict) -> Optional[int]:
    text = str(entry.get("chapter") or "").strip()
    return int(text) if text.isdigit() else None


def fetch_series(url: str) -> Optional[dict]:
    """Returns {"title", "total", "chapters": [{"n","title"}, ...], "source": "scraped"} on
    success, or None on ANY failure. Side chapters such as 223.9 are not listed."""
    try:
        slug = _slug(url)
        if not slug:
            return None
        page, manga_id = _series_page(slug)
        numbers = sorted({n for e in _chapter_list(manga_id) if (n := _whole_number(e)) is not None})
        if not numbers:
            return None
        heading = page.select_one("h1")
        series_url = f"{HOME_URL}manga/{slug}/"
        return {"title": heading.get_text(strip=True) if heading else slug,
                "total": len(numbers),
                "chapters": [{"n": n, "title": f"Chapter {n}"} for n in numbers],
                "source": "scraped", **meta.from_soup(page, series_url)}
    except Exception:  # noqa: BLE001 - a scrape failure must never block project creation
        return None


def _chapter_id(url: str, slug: str, chapter_no: str) -> tuple[str, str]:
    """(chapter id, chapter URL) for `chapter_no`. A pasted /read/ URL for that same chapter is used
    as it is; anything else (a series URL, another chapter's URL) is looked up in the series' list."""
    pasted = _CHAPTER_URL_RE.search(url or "")
    if pasted and pasted.group(1).replace("-", ".") == str(chapter_no):
        return pasted.group(2), url
    _, manga_id = _series_page(slug)
    for entry in _chapter_list(manga_id):
        if str(entry.get("chapter")).strip() == str(chapter_no).strip() and entry.get("id"):
            return str(entry["id"]), entry.get("url") or f"{HOME_URL}read/{slug}/"
    raise RuntimeError(f"roliascan.com doesn't list chapter {chapter_no} of {slug}.")


def download_chapter(url: str, chapter_no: str, dest: Path,
                     log: Callable[[str], None] = lambda s: None) -> Path:
    """Fetch ONE chapter's images and assemble them into a single PDF under `dest`."""
    slug = _slug(url)
    if not slug:
        raise RuntimeError("Couldn't find a roliascan.com series slug in that URL.")
    if not chapter_no:
        raise RuntimeError("Enter the chapter number.")

    chapter_id, chapter_url = _chapter_id(url, slug, chapter_no)
    log(f"Fetching {chapter_url}")
    data = _get(f"{HOME_URL}auth/chapter-content", params={"chapter_id": chapter_id},
                headers={**_HEADERS, "Referer": chapter_url}).json()
    images = data.get("images") or []
    if not data.get("success") or not images:
        raise RuntimeError(f"No images found on {chapter_url}.")
    log(f"Found {len(images)} image(s) - downloading…")

    pages = fetch_pages(images, {**_HEADERS, "Referer": chapter_url}, _TIMEOUT, log)
    pdf_path = dest / f"{slug}-chapter-{chapter_no}.pdf"
    images_to_pdf(pages, pdf_path)
    log(f"Wrote {pdf_path.name} ({len(pages)} page(s))")
    return pdf_path

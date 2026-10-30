"""mgeko.cc adapter: series-metadata scraping + chapter image download over plain HTTP.

The series page holds the title and `/manga/<slug>/all-chapters/` lists every chapter. A chapter
page's images are the `img#image-N` tags inside `div#chapter-reader`; the credits banner, the logo
and the loading spinners carry no such id. Downloaded images are assembled into a single PDF so
everything after Stitch PDF runs the same code path as the other sources. Alternate uploads of a
chapter ("chapter-16-fixed") are ignored in favour of the plain one.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

import requests
from bs4 import BeautifulSoup

import app_settings
from . import meta
from .images import fetch_pages, images_to_pdf

SITE_NAME = "mgeko.cc"
HOME_URL = "https://www.mgeko.cc/"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}
_TIMEOUT = (4, 15)  # (connect, read) seconds

_SLUG_RE = re.compile(r"mgeko\.cc/(?:manga/([a-z0-9-]+)|reader/en/([a-z0-9-]+?)-chapter-\d+(?:-[a-z]+)*-eng-li)", re.I)
_IMAGE_ID_RE = re.compile(r"image-(\d+)$")


def matches(url: str) -> bool:
    return "mgeko.cc" in (url or "").lower()


def _slug(url: str) -> Optional[str]:
    m = _SLUG_RE.search(url or "")
    return (m.group(1) or m.group(2)) if m else None


def _session_headers() -> dict:
    """Headers for mgeko.cc pages: with a saved browser session (app_settings.json, "site_sessions")
    the Cloudflare cookie and the user agent it was issued to. Not used for the image host."""
    saved = app_settings.site_session("mgeko")
    headers = dict(_HEADERS)
    if saved.get("user_agent"):
        headers["User-Agent"] = saved["user_agent"]
    if saved.get("cookie"):
        headers["Cookie"] = _clean_cookie(saved["cookie"])
    return headers


def _clean_cookie(raw: str) -> str:
    """`cf_clearance=<value>` from whatever was pasted: the bare value, `name=value`, or a copied
    devtools row such as `cf_clearance: <value>` (also after a `name=`)."""
    value = raw.strip()
    while True:
        m = re.match(r"cf_clearance\s*[=:]\s*", value, re.I)
        if not m:
            break
        value = value[m.end():]
    return f"cf_clearance={value}"


def _get(url: str) -> str:
    resp = requests.get(url, headers=_session_headers(), timeout=_TIMEOUT)
    if resp.status_code == 403 and resp.headers.get("cf-mitigated") == "challenge":
        raise RuntimeError(f"Couldn't load {url}: mgeko.cc is behind a Cloudflare browser check. "
                           "Save a fresh cf_clearance cookie and your browser's user agent under "
                           '"site_sessions" -> "mgeko" in app_settings.json (it expires after a while).')
    if resp.status_code != 200 or not resp.text:
        raise RuntimeError(f"Couldn't load {url} (HTTP {resp.status_code}).")
    return resp.text


def fetch_series(url: str) -> Optional[dict]:
    """Returns {"title", "total", "chapters": [{"n","title"}, ...], "source": "scraped"} on
    success, or None on ANY failure."""
    try:
        slug = _slug(url)
        if not slug:
            return None
        series_url = f"{HOME_URL}manga/{slug}/"
        page = BeautifulSoup(_get(series_url), "html.parser")
        heading = page.select_one("h1.novel-title")
        listing = _get(f"{series_url}all-chapters/")
        numbers = sorted({int(n) for n in re.findall(
            rf'href="/reader/en/{re.escape(slug)}-chapter-(\d+)-eng-li/"', listing)})
        if not numbers:
            return None
        chapters = [{"n": n, "title": f"Chapter {n}"} for n in numbers]
        return {"title": heading.get_text(strip=True) if heading else slug, "total": len(chapters),
                "chapters": chapters, "source": "scraped",
                **meta.from_soup(page, series_url)}
    except Exception:  # noqa: BLE001 - a scrape failure must never block project creation
        return None


def _chapter_images(html: str) -> list[str]:
    """Page image URLs in reading order, which is the number in each image's id."""
    soup = BeautifulSoup(html, "html.parser")
    numbered = []
    for img in soup.select("div#chapter-reader img[id][src]"):
        if m := _IMAGE_ID_RE.match(img["id"]):
            numbered.append((int(m[1]), img["src"].strip()))
    return [src for _, src in sorted(numbered)]


def download_chapter(url: str, chapter_no: str, dest: Path,
                     log: Callable[[str], None] = lambda s: None) -> Path:
    """Fetch ONE chapter's images and assemble them into a single PDF under `dest`."""
    slug = _slug(url)
    if not slug:
        raise RuntimeError("Couldn't find a mgeko.cc series slug in that URL.")
    if not chapter_no:
        raise RuntimeError("Enter the chapter number.")
    chapter_url = f"{HOME_URL}reader/en/{slug}-chapter-{chapter_no}-eng-li/"
    log(f"Fetching {chapter_url}")
    images = _chapter_images(_get(chapter_url))
    if not images:
        raise RuntimeError(f"No images found on {chapter_url}.")
    log(f"Found {len(images)} image(s) - downloading…")

    pdf_path = dest / f"{slug}-chapter-{chapter_no}.pdf"
    images_to_pdf(fetch_pages(images, _HEADERS, _TIMEOUT, log), pdf_path)
    log(f"Wrote {pdf_path.name} ({len(images)} page(s))")
    return pdf_path

"""kingofshojo.com adapter: series-metadata scraping + chapter image download.

A chapter page embeds its authoritative image list as JSON in an inline
`ts_reader.run({...})` call (`sources[0].images`); `#readerarea img` tags are
the fallback if that script is ever restructured. Downloaded images are
assembled into a single PDF so downstream (Stitch PDF, and everything after
it) runs the exact same code path as a webtoons.com download.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Callable, Optional

import requests
from bs4 import BeautifulSoup
from PIL import Image

SITE_NAME = "kingofshojo.com"
HOME_URL = "https://kingofshojo.com/"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
}
_TIMEOUT = (4, 15)  # (connect, read) seconds - chapter pages/images are heavier than webtoons_meta's

_SLUG_RE = re.compile(r"kingofshojo\.com/(?:manga/([a-z0-9-]+)|([a-z0-9-]+)-chapter-[0-9.]+)/?", re.I)
_CHAPTER_RUN_RE = re.compile(r"ts_reader\.run\((\{.*?\})\);", re.S)


def matches(url: str) -> bool:
    return "kingofshojo.com" in (url or "").lower()


def _slug(url: str) -> Optional[str]:
    m = _SLUG_RE.search(url or "")
    if not m:
        return None
    return m.group(1) or m.group(2)


# --------------------------------------------------------------------------- #
# series metadata
# --------------------------------------------------------------------------- #
def _extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.select_one("h1.entry-title, h1")
    return h1.get_text(strip=True) if h1 and h1.get_text(strip=True) else "Untitled series"


def fetch_series(url: str) -> Optional[dict]:
    """Returns {"title", "total", "chapters": [{"n","title"}, ...], "source":
    "scraped"} on success, or None on ANY failure. The chapter list's own
    anchors (`<slug>-chapter-<n>`) give both ends of the range directly - more
    robust than the page's "First:"/"Latest:" links, whose href is a client-
    side placeholder until JS runs."""
    try:
        slug = _slug(url)
        if not slug:
            return None
        list_url = f"https://kingofshojo.com/manga/{slug}/"
        resp = requests.get(list_url, headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code != 200 or not resp.text:
            return None

        nums = sorted(set(int(n) for n in
                          re.findall(rf"{re.escape(slug)}-chapter-(\d+)/?[\"']", resp.text)))
        if not nums:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        title = _extract_title(soup)
        chapters = [{"n": n, "title": f"Chapter {n}"} for n in range(nums[0], nums[-1] + 1)]
        return {"title": title, "total": len(chapters), "chapters": chapters,
                "source": "scraped", "list_url": list_url}
    except Exception:  # noqa: BLE001 - a scrape failure must never block project creation
        return None


# --------------------------------------------------------------------------- #
# chapter download
# --------------------------------------------------------------------------- #
def _chapter_images(html: str) -> list[str]:
    m = _CHAPTER_RUN_RE.search(html)
    if m:
        try:
            sources = json.loads(m.group(1)).get("sources") or []
            images = sources[0].get("images") if sources else None
            if images:
                return images
        except (ValueError, IndexError, KeyError, AttributeError):
            pass
    soup = BeautifulSoup(html, "html.parser")
    area = soup.select_one("#readerarea")
    return [img["src"] for img in area.select("img[src]")] if area else []


def download_chapter(url: str, chapter_no: str, dest: Path,
                      log: Callable[[str], None] = lambda s: None) -> Path:
    """Fetch ONE chapter's images and assemble them into a single PDF under
    `dest`. Returns the PDF's path."""
    slug = _slug(url)
    if not slug:
        raise RuntimeError("Couldn't find a kingofshojo.com series slug in that URL.")
    if not chapter_no:
        raise RuntimeError("Enter the chapter number.")

    chapter_url = f"https://kingofshojo.com/{slug}-chapter-{chapter_no}/"
    log(f"Fetching {chapter_url}")
    resp = requests.get(chapter_url, headers=_HEADERS, timeout=_TIMEOUT)
    if resp.status_code != 200 or not resp.text:
        raise RuntimeError(f"Couldn't load {chapter_url} (HTTP {resp.status_code}).")

    images = _chapter_images(resp.text)
    if not images:
        raise RuntimeError(f"No images found on {chapter_url}.")
    log(f"Found {len(images)} image(s) - downloading…")

    pages = []
    for i, img_url in enumerate(images, 1):
        r = requests.get(img_url, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        pages.append(Image.open(io.BytesIO(r.content)).convert("RGB"))
        if i % 20 == 0 or i == len(images):
            log(f"  {i}/{len(images)} downloaded")

    pdf_path = dest / f"{slug}-chapter-{chapter_no}.pdf"
    pages[0].save(pdf_path, save_all=True, append_images=pages[1:])
    log(f"Wrote {pdf_path.name} ({len(pages)} page(s))")
    return pdf_path

"""Series cover and description, read from the series page's own meta tags.

Every adapter already loads the series page; `from_soup` pulls the cover image URL and the blurb
out of it (`og:image` / `og:description`, with the plain `description` meta as a fallback) so
`fetch_series` can return them next to the title and chapter list.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup


def _meta(soup: BeautifulSoup, *keys: str) -> Optional[str]:
    for key in keys:
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if tag and (tag.get("content") or "").strip():
            return tag["content"].strip()
    return None


def from_soup(soup: BeautifulSoup, page_url: str) -> dict:
    """{"cover_url", "description"} (each None when the page has none)."""
    cover = _meta(soup, "og:image", "twitter:image")
    description = _meta(soup, "og:description", "description", "twitter:description")
    return {"cover_url": urljoin(page_url, cover) if cover else None,
            "description": " ".join(description.split()) if description else None}

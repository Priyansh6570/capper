"""Best-effort webtoons.com series metadata scraper for the "New Project" form.

`fetch_series(url)` NEVER raises and never blocks for long: on any failure it
returns None, and the caller falls back to a manual "enter chapter range 1..N"
input. This is a convenience, not a dependency - webtoons.com's markup can
change at any time.

Strategy (verified against a live series page): the series `/list?title_no=`
page is sorted NEWEST-FIRST, so the highest `episode_no`/`data-episode-no` on
page 1 IS the total episode count - no pagination crawl needed. Only the
handful of episodes visible on page 1 get real titles; the rest are
synthesized as "Episode N".
"""
from __future__ import annotations

import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://www.webtoons.com/",
}
_TIMEOUT = (4, 8)  # (connect, read) seconds


def _normalize_list_url(url: str) -> Optional[str]:
    """A chapter/viewer URL -> the series' /list?title_no= URL. Accepts a list
    URL as-is. Returns None if no title_no can be found anywhere in the URL."""
    url = (url or "").strip()
    m = re.search(r"title_no=(\d+)", url)
    if not m:
        return None
    title_no = m.group(1)
    m2 = re.match(r"^(https?://[^/]+/[^/]+/[^/]+/[^/?#]+)", url)
    base = m2.group(1) if m2 else "https://www.webtoons.com/en/genre/series"
    return f"{base}/list?title_no={title_no}"


def _extract_title(soup: BeautifulSoup, fallback_url: str) -> str:
    h1 = soup.select_one("h1.subj")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        return og["content"].strip()
    m = re.search(r"webtoons\.com/[^/]+/[^/]+/([^/?#]+)", fallback_url)
    return m.group(1).replace("-", " ").title() if m else "Untitled series"


def _episode_numbers(soup: BeautifulSoup, html: str) -> list[int]:
    nums: list[int] = []
    for li in soup.select("#_listUl li[data-episode-no]"):
        try:
            nums.append(int(li["data-episode-no"]))
        except (KeyError, ValueError):
            continue
    if not nums:
        for a in soup.select("#_listUl a[href]"):
            m = re.search(r"episode_no=(\d+)", a["href"])
            if m:
                nums.append(int(m.group(1)))
    if not nums:
        nums = [int(x) for x in re.findall(r"episode_no=(\d+)", html)]
    return nums


def _episode_titles(soup: BeautifulSoup) -> dict[int, str]:
    titles: dict[int, str] = {}
    for li in soup.select("#_listUl li[data-episode-no]"):
        try:
            n = int(li["data-episode-no"])
        except (KeyError, ValueError):
            continue
        subj = li.select_one(".subj, .subj span")
        if subj and subj.get_text(strip=True):
            titles[n] = subj.get_text(strip=True)
    return titles


def fetch_series(url: str) -> Optional[dict]:
    """Returns {"title", "total", "chapters": [{"n","title"}, ...], "source":
    "scraped"} on success, or None on ANY failure (bad URL, network error,
    unexpected markup, zero episodes found)."""
    try:
        list_url = _normalize_list_url(url)
        if not list_url:
            return None
        resp = requests.get(list_url, headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code != 200 or not resp.text:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        nums = _episode_numbers(soup, resp.text)
        if not nums:
            return None
        total = max(nums)
        titles = _episode_titles(soup)

        title = _extract_title(soup, list_url)
        chapters = [{"n": n, "title": titles.get(n, f"Episode {n}")} for n in range(1, total + 1)]
        return {"title": title, "total": total, "chapters": chapters, "source": "scraped",
                "list_url": list_url}
    except Exception:  # noqa: BLE001 - a scrape failure must never block project creation
        return None

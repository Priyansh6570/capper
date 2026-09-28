"""webtoons.com adapter: series-metadata scraping + chapter download via the
external `webtoon-downloader` CLI (installed into this app's own .venv).

`fetch_series()` NEVER raises and never blocks for long: on any failure it
returns None, and the caller falls back to a manual "enter chapter range
1..N" input. This is a convenience, not a dependency - webtoons.com's markup
can change at any time.

Strategy (verified against a live series page): the series `/list?title_no=`
page is sorted NEWEST-FIRST, so the highest `episode_no`/`data-episode-no` on
page 1 IS the total episode count - no pagination crawl needed. Only the
handful of episodes visible on page 1 get real titles; the rest are
synthesized as "Episode N".
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

import requests
from bs4 import BeautifulSoup

from config import NO_CONSOLE
from sites.errors import DownloadTimeout

SITE_NAME = "webtoons.com"
HOME_URL = "https://www.webtoons.com/"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://www.webtoons.com/",
}
_TIMEOUT = (4, 8)  # (connect, read) seconds
_DOWNLOAD_TIMEOUT_S = 1800  # hard cap on a single webtoon-downloader run (30 min)


def matches(url: str) -> bool:
    return "webtoon" in (url or "").lower()


# --------------------------------------------------------------------------- #
# series metadata
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# chapter download
# --------------------------------------------------------------------------- #
def _cli() -> Optional[list[str]]:
    """Locate the webtoon-downloader CLI. `pip install webtoon-downloader` only
    ever installs a console-script entry point - the package has no
    `__main__.py`, so `python -m webtoon_downloader` ALWAYS fails with
    "'webtoon_downloader' is a package and cannot be directly executed",
    regardless of how it was installed. So look for the entry-point script
    next to the running interpreter first (that's where `pip install` puts it
    when run inside this app's own .venv, which is how the README says to
    install it - and start.bat launches app.py with the bare venv python, not
    an activated venv, so .venv\\Scripts is NOT on PATH for this process),
    then fall back to PATH for a global/other-env install.
    Returns the command prefix, or None if the tool isn't installed."""
    venv_script = Path(sys.executable).parent / (
        "webtoon-downloader.exe" if os.name == "nt" else "webtoon-downloader")
    if venv_script.exists():
        return [str(venv_script)]
    exe = shutil.which("webtoon-downloader")
    if exe:
        return [exe]
    return None


_OUT_FLAG: Optional[str] = None


def _out_flag(base: list[str]) -> str:
    """The output-directory flag for the installed webtoon-downloader: newer builds
    use `--out` (older `--dest` is rejected/deprecated). Probed once from --help and
    cached; defaults to `--out`."""
    global _OUT_FLAG
    if _OUT_FLAG is None:
        _OUT_FLAG = "--out"
        try:
            h = subprocess.run(base + ["--help"], capture_output=True, text=True,
                               timeout=30, **NO_CONSOLE)
            text = (h.stdout or "") + (h.stderr or "")
            if "--out" not in text and "--dest" in text:
                _OUT_FLAG = "--dest"
        except Exception:  # noqa: BLE001 - probe failure -> keep the modern default
            pass
    return _OUT_FLAG


def _newest_pdf(dest: Path) -> Optional[Path]:
    """The most recently written PDF anywhere under `dest` (webtoon-downloader may
    nest it under a series/chapter subfolder)."""
    pdfs = sorted(dest.rglob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    return pdfs[0] if pdfs else None


def download_chapter(url: str, chapter_no: str, dest: Path,
                      log: Callable[[str], None] = lambda s: None) -> Path:
    """Fetch ONE chapter into `dest` as a PDF with webtoon-downloader. When a
    chapter number is given, --start/--end are pinned to it so exactly that
    chapter is pulled. Returns the downloaded PDF's path."""
    base = _cli()
    if base is None:
        raise RuntimeError(
            "webtoon-downloader is not installed into this app's .venv. "
            "Install it with:\n"
            "    .venv\\Scripts\\pip install webtoon-downloader\n"
            "(run from the project root) - a plain `pip install` may land in "
            "a different Python and won't be found by this app.")
    cmd = base + [url, _out_flag(base), str(dest), "--save-as", "pdf"]
    if chapter_no:
        cmd += ["--start", str(chapter_no), "--end", str(chapter_no)]
    log("$ " + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=_DOWNLOAD_TIMEOUT_S, **NO_CONSOLE)
    except subprocess.TimeoutExpired:
        raise DownloadTimeout(
            f"webtoon-downloader timed out after {_DOWNLOAD_TIMEOUT_S // 60} min.")

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()
        tail = tail[-1000:] if tail else "(no output)"
        raise RuntimeError(f"webtoon-downloader exited {proc.returncode}:\n{tail}")

    pdf = _newest_pdf(dest)
    if pdf is None:
        raise RuntimeError(f"Download finished but no PDF was found under {dest}. "
                           f"Check that this webtoon-downloader build supports "
                           f"--save-as pdf.")
    return pdf

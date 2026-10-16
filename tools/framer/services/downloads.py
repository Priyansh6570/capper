"""Downloading a chapter through a site adapter (see sites/) into the chapter's work folder."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

import projects
import sites
from services.chapter_files import download_dir, pdf_page_count
from services.common import Refused, sanitize_chapter


def _download_chapter(url: str, chapter_no: str, raw_id: str, slug: str | None, ch_no: str | None,
                      log: Callable[[str], None] | None = None) -> dict:
    """Download ONE chapter as a PDF from whichever site adapter (see tools/framer/sites/)
    matches `url`; `log` also receives each step as it happens. Returns {chapter_id, pdf,
    page_count, steps}."""
    if not url:
        raise Refused("Paste a webtoons.com, kingofshojo.com or mgeko.cc chapter URL.")
    adapter = sites.detect(url)
    if adapter is None:
        raise Refused("That doesn't look like a webtoons.com, kingofshojo.com or mgeko.cc URL.")
    if not raw_id and not chapter_no:
        raise Refused("Enter the chapter number (or a chapter id).")
    chapter_id = sanitize_chapter(raw_id or chapter_no)

    dest = download_dir(chapter_id)
    steps = [f"Downloading {adapter.SITE_NAME} chapter {chapter_no or '(from URL)'} "
             f"into {dest} …"]
    def note(line: str) -> None:
        steps.append(line)
        if log:
            log(line)

    try:
        pdf = _fetch_chapter_pdf(adapter, url, chapter_no, dest, slug, ch_no, note)
    except sites.DownloadTimeout as e:
        raise Refused(str(e), 504) from e
    except Exception as e:  # noqa: BLE001 - surface the adapter's own message to the UI
        raise Refused(str(e), 500) from e

    for old in dest.rglob("*"):
        if old.is_file() and old != pdf:
            old.unlink()
    pages = pdf_page_count(pdf)
    steps.append(f"Downloaded {pdf.name} — {pages} page(s):\n    {pdf}")
    if slug and ch_no:
        projects.set_status(slug, ch_no, "downloaded")
    return {"chapter_id": chapter_id, "pdf": str(pdf), "page_count": pages, "steps": steps}


def _fetch_chapter_pdf(adapter, url: str, chapter_no: str, dest: Path, slug: str | None, ch_no: str | None,
                       log: Callable[[str], None]) -> Path:
    """The chapter's PDF in `dest`. A project that remembers its source keeps its downloads in the
    project folder: a chapter stored there is copied from it instead of being downloaded again, and a
    fresh download is stored there once complete."""
    keeps_copy = bool(slug and ch_no and projects.load(slug)["site"])
    stored = projects.stored_pdf(slug, ch_no) if keeps_copy else None
    if stored:
        log(f"Using the copy in the project folder ({stored.name})")
        return Path(shutil.copy2(stored, dest / stored.name))
    pdf = adapter.download_chapter(url, chapter_no, dest, log=log)
    if keeps_copy:
        projects.store_pdf(slug, ch_no, pdf)
    return pdf

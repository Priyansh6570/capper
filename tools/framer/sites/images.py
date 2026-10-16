"""Shared by the adapters that download a chapter as loose images."""
from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import requests
from PIL import Image

from config import PAGE_DPI

DOWNLOAD_WORKERS = 8


def fetch_pages(urls: list[str], headers: dict, timeout: tuple[int, int],
                log: Callable[[str], None]) -> list[Image.Image]:
    """The images at `urls`, in the same order. A shared session and a small thread pool keep
    the per-request round trips from dominating a chapter of 100+ images."""
    with requests.Session() as session:
        session.headers.update(headers)

        def fetch(url: str) -> Image.Image:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return Image.open(io.BytesIO(response.content)).convert("RGB")

        pages = []
        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
            for done, page in enumerate(pool.map(fetch, urls), 1):
                pages.append(page)
                if done % 20 == 0 or done == len(urls):
                    log(f"  {done}/{len(urls)} downloaded")
    return pages


def images_to_pdf(pages: list[Image.Image], path: Path) -> None:
    """Pillow treats pixel size as point size (72 DPI) unless told otherwise, and Stitch PDF
    (stages/s1_pdf_to_pages.py) re-renders pages at PAGE_DPI, which would upscale every page
    again and run out of memory on a full chapter. Passing PAGE_DPI makes that render 1:1."""
    pages[0].save(path, save_all=True, append_images=pages[1:], resolution=PAGE_DPI)

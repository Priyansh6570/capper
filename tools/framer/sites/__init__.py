"""Per-site adapters: each module here knows how to read one manhwa/manhua
site's series metadata and pull a chapter's images into a PDF for the
existing Stitch-PDF flow. A URL's site is auto-detected (see `detect`) - the
rest of the app never branches on a site name.

Add a new site by adding a module with the same four names (SITE_NAME,
HOME_URL, matches, fetch_series, download_chapter) and registering it below.
"""
from __future__ import annotations

from .errors import DownloadTimeout
from . import kingofshojo, webtoons

ADAPTERS = [webtoons, kingofshojo]


def detect(url: str):
    """The adapter module whose site `url` belongs to, or None."""
    for adapter in ADAPTERS:
        if adapter.matches(url):
            return adapter
    return None


__all__ = ["ADAPTERS", "DownloadTimeout", "detect"]

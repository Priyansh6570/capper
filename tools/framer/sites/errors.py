"""Shared exception types for site adapters (kept out of __init__.py so
adapter modules can import it without a circular import on the package)."""
from __future__ import annotations


class DownloadTimeout(RuntimeError):
    """A chapter download exceeded its adapter's time budget."""

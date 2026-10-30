"""Thumbnails as YouTube takes them: a JPEG of at most 2 MB, 1280x720 at most."""
from __future__ import annotations

import io
from pathlib import Path
from typing import BinaryIO

from PIL import Image

MAX_BYTES = 2_000_000
MAX_SIZE = (1280, 720)
QUALITIES = (92, 85, 75, 60, 40)


def jpeg_for_youtube(source: str | Path | BinaryIO) -> bytes:
    with Image.open(source) as opened:
        image = opened.convert("RGB")
    image.thumbnail(MAX_SIZE)
    for quality in QUALITIES:
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=quality)
        if buffer.tell() <= MAX_BYTES:
            break
    return buffer.getvalue()

"""What the script step derives from a chapter's panels, kept on disk beside its Claude state:
the text read from each panel and the downscaled copies sent to Claude. A re-run, or the same
chapter in another mode, reuses both. The panel files themselves are only read."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Callable

from PIL import Image

import ocr
from atomic_io import atomic_write_json, read_json_with_backup_fallback
from manifest import Panel

JPEG_QUALITY = 80
SAVE_EVERY = 20             # panels read between saves of the text cache


def _signature(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


class PanelCache:
    def __init__(self, folder: Path):
        self.folder = folder
        self.text_path = folder / "ocr.json"

    def texts(self, panels: list[Panel], on_read: Callable[[int, int], None] = lambda done, total: None) -> dict[str, list[dict]]:
        """{panel id: [{text, box}]} for every panel, reading only the ones not cached for their
        current file. `on_read(done, total)` is called as the missing ones are read."""
        try:
            cached = read_json_with_backup_fallback(self.text_path)
        except (OSError, ValueError):
            cached = {}
        stale = [p for p in panels if cached.get(p.id, {}).get("signature") != _signature(Path(p.crop))]
        for done, panel in enumerate(stale, start=1):
            cached[panel.id] = {"signature": _signature(Path(panel.crop)), "blocks": ocr.read_panel(Path(panel.crop))}
            on_read(done, len(stale))
            if done % SAVE_EVERY == 0 or done == len(stale):
                atomic_write_json(self.text_path, cached, backup=False)
        return {p.id: cached[p.id]["blocks"] for p in panels}

    def image(self, panel: Panel, side: int) -> bytes:
        """The panel as a JPEG whose long edge is at most `side`."""
        source, copy = Path(panel.crop), self.folder / f"send_{side}" / f"{panel.id}.jpg"
        if not copy.is_file() or copy.stat().st_mtime < source.stat().st_mtime:
            with Image.open(source) as image:
                image = image.convert("RGB")
                image.thumbnail((side, side))
                buffer = io.BytesIO()
                image.save(buffer, "JPEG", quality=JPEG_QUALITY)
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(buffer.getvalue())
        return copy.read_bytes()

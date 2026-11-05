"""Offline text reading for panels: speech bubbles and captions with their place in the panel.

EasyOCR runs on the GPU when torch sees one and on the CPU otherwise (or when the GPU runs out
of memory). Its models live in `models/easyocr` under the app folder and are fetched once, by
deps.py at install time or on first use, so a read never needs the network afterwards.
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from PIL import Image

from config import ROOT

MODEL_DIR = ROOT / "models" / "easyocr"
MIN_ALNUM = 2               # text with fewer letters or digits is a stray mark, not dialogue

_LOCK = threading.Lock()
_reader = None


def _build(gpu: bool):
    import easyocr
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    return easyocr.Reader(["en"], gpu=gpu, model_storage_directory=str(MODEL_DIR), verbose=False)


def _gpu_available() -> bool:
    import torch
    return torch.cuda.is_available()


def ensure_models() -> str:
    """Fetch the models if they are missing; returns the device a read would use."""
    with _LOCK:
        global _reader
        if _reader is None:
            _reader = _build(_gpu_available())
        return "gpu" if _reader.device != "cpu" else "cpu"


def _read(image: np.ndarray) -> list:
    global _reader
    if _reader is None:
        _reader = _build(_gpu_available())
    try:
        return _reader.readtext(image, paragraph=True)
    except RuntimeError:
        if _reader.device == "cpu":
            raise
        import torch
        torch.cuda.empty_cache()
        _reader = _build(False)
        return _reader.readtext(image, paragraph=True)


def read_panel(path: Path) -> list[dict]:
    """The text blocks of one panel in reading order, each `{text, box}` with `box` the
    [left, top, right, bottom] share of the panel's width and height."""
    with Image.open(path) as panel:
        image = np.asarray(panel.convert("RGB"))
    height, width = image.shape[:2]
    with _LOCK:
        found = _read(image)
    blocks = []
    for corners, text in found:
        text = " ".join(text.split())
        if sum(c.isalnum() for c in text) < MIN_ALNUM:
            continue
        xs, ys = [p[0] for p in corners], [p[1] for p in corners]
        box = [min(xs) / width, min(ys) / height, max(xs) / width, max(ys) / height]
        blocks.append({"text": text, "box": [round(min(1.0, max(0.0, v)), 3) for v in box]})
    return sorted(blocks, key=lambda b: (b["box"][1], b["box"][0]))

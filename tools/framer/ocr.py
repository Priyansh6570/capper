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
_gpu_failed = False         # a CUDA error (usually out of memory) was seen: stay on the CPU for this process


def _build(gpu: bool):
    """A reader on the GPU, or on the CPU when the GPU is missing or fails to take the models."""
    global _gpu_failed
    import easyocr
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if gpu and not _gpu_failed:
        try:
            return easyocr.Reader(["en"], gpu=True, model_storage_directory=str(MODEL_DIR), verbose=False)
        except Exception:  # noqa: BLE001 - CUDA out of memory surfaces as several exception types
            _gpu_failed = True
            _release_gpu()
    return easyocr.Reader(["en"], gpu=False, model_storage_directory=str(MODEL_DIR), verbose=False)


def _release_gpu() -> None:
    try:
        import gc
        import torch
        gc.collect()
        torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001 - after a CUDA error even this call can raise
        pass


def _gpu_available() -> bool:
    if _gpu_failed:
        return False
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
    except Exception:  # noqa: BLE001 - CUDA errors are not always a RuntimeError (AcceleratorError)
        global _gpu_failed
        if _reader.device == "cpu":
            raise
        _gpu_failed = True
        _reader = None
        _release_gpu()
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

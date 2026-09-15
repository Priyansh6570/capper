"""Stage 4 - Remove on-panel text (inpainting).  [STUB]  Runs on: Kaggle GPU.

REPLACE with IOPaint + a LaMa model, using the bubble bboxes from stage 2 as
masks, so original text is painted out and you narrate over clean art. For tall
webtoon pages, inpaint per-panel-crop to avoid OOM on the 16GB GPU.

The stub copies each page through unchanged as its 'clean' version.
"""
import shutil
from pathlib import Path

from config import chapter_work_dir
from manifest import Manifest


def run(m: Manifest) -> Manifest:
    clean_dir = chapter_work_dir(m.chapter_id) / "clean"
    clean_dir.mkdir(parents=True, exist_ok=True)
    for page in m.pages:
        dst = clean_dir / Path(page.image).name
        shutil.copyfile(page.image, dst)
        page.clean_image = str(dst)
    print(f"  (stub) passed through {len(m.pages)} pages as 'clean'")
    m.stage = "clean"
    return m

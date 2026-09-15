# Kaggle GPU stages

Stages 2, 4, and 6 run on a Kaggle free GPU, not the laptop. Each is a
self-contained script that works over one chapter's `work/<chapter>/` folder:
you sync the inputs **up** to Kaggle, run the script, and sync the outputs
**back down** into `work/<chapter>/_kaggle/`, where the local loader stage
ingests them. The local `run(m) -> m` stages stay unchanged — the GPU work is
entirely behind the file contract.

Currently here:

| Script | Stage | Input it reads | Output it writes |
|--------|-------|----------------|------------------|
| `stage2_webtoon.py` | 2 (webtoon) | the stitched strip `strip_001.png` | `panels.json` + `crops/` + `debug/` |

(Manga stage 2 and stages 4/6 will follow the same round-trip.)

## Round-trip for `stage2_webtoon.py`

### 1. Upload (laptop → Kaggle)

Stage 1 must have run locally with `--type webtoon`, producing
`work/<chapter>/pages/strip_001.png`.

- Create a **Kaggle Dataset** containing that strip (the full-res
  `strip_001.png`, not the `_preview`). One strip per chapter.
- Note the dataset's mount path, e.g. `/kaggle/input/<your-dataset-slug>/`.

### 2. Run (on Kaggle)

- New Notebook → **Accelerator: GPU**, **Internet: On** → add the dataset.
- Add the script (paste `stage2_webtoon.py`, or attach this repo / a script
  dataset) and install the pinned deps from the commented `pip install` block at
  the top of the file.
- Point it at your data and run:

  ```python
  import os
  os.environ["INPUT_DIR"] = "/kaggle/input/<your-dataset-slug>"
  # default OUTPUT_DIR is /kaggle/working/stage2_out
  %run stage2_webtoon.py
  ```

  It auto-discovers `strip_*.png` under `INPUT_DIR` (recursively), so the exact
  subfolder usually doesn't matter.

What it does: detection on **GPU** (tiled RT-DETR-v2), beat segmentation, crops,
debug overlay, and `panels.json`, then a best-effort **CPU** OCR pass that fills
`dialogue`. Detection/beats/crops/debug/`panels.json` are written **before** OCR,
so even if OCR fails you still get a full debug overlay and a valid (empty-text)
contract. Re-run after fixing OCR deps to fill the text.

### 3. Download (Kaggle → laptop)

From the notebook's **Output**, download `stage2_out/` and extract its
**contents** into the chapter's `_kaggle` folder so you end up with:

```
work/<chapter>/_kaggle/panels.json
work/<chapter>/_kaggle/crops/0001.png, 0002.png, ...
```

(Optionally keep `stage2_out/debug/` around to eyeball reading order — it isn't
read by the loader.)

### 4. Ingest (laptop)

```bash
python orchestrator.py --chapter <chapter>
```

The local stage 2 (`stages/s2_detect_panels.py`) is a format-agnostic loader: it
copies `_kaggle/crops/*` into `work/<chapter>/panels/` and builds the manifest's
panels from `panels.json`. If `_kaggle/` is missing it errors and points you
back here.

## Notes

- **OCR is CPU by default.** The PaddlePaddle *GPU* wheel must match the
  notebook's exact CUDA build and often fails to install; CPU OCR is fine for one
  strip's worth of bubbles. To switch OCR engines (e.g. `python-doctr`), edit
  only `OcrEngine` in the script.
- **`page` is always `1`** for webtoons — a chapter is a single stitched strip,
  matching the single `Page(index=1)` stage 1 emits.
- The webtoon-vs-manga difference lives in these Kaggle scripts, **never** in the
  local loader.

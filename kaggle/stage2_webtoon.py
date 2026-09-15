"""Stage 2 (WEBTOON) - panel/beat + bubble detection, reading order, English OCR.

RUN THIS ON KAGGLE (GPU + internet), not locally. It is the detection half of
the pipeline's stage 2: it consumes a stitched vertical-webtoon strip (produced
by local stage 1, `strip_001.png`) and writes the *exact* contract that the
local loader `stages/s2_detect_panels.py` ingests:

    /kaggle/working/stage2_out/
        crops/NNNN.png            # one image per reading-order beat (unique names)
        panels.json               # { "panels": [ {order,page,bbox,narration,
                                  #                dialogue,speaker,crop_file}, ... ] }
        debug/strip_boxes.png     # full strip with boxes + order numbers
        debug/preview.png         # downscaled preview of the above

After it finishes: download `stage2_out/` and extract it to
`work/<chapter>/_kaggle/` on the laptop (so you get `_kaggle/panels.json` and
`_kaggle/crops/`), then run the orchestrator - stage 2 there is just a loader.

The webtoon-vs-manga difference lives HERE (and in the manga notebook), never in
the local loader. `page` is always 1 because a webtoon chapter is a single strip.

Detector: ogkalu's comic-translate RT-DETR-v2 text/bubble model
    HF: ogkalu/comic-text-and-bubble-detector
    classes: 0=bubble, 1=text_bubble, 2=text_free
    (RT-DETR-v2 r50vd, trained on manga/webtoon/western incl. tall webtoons)

Pipeline:
  1. detect: tile the very tall strip into overlapping vertical windows, run the
     detector per window, map boxes back to full-strip coords, class-aware NMS,
     then an extra TEXT-box dedupe (overlapping tiles see the same bubble twice
     with boxes offset enough to survive NMS, which doubles the OCR otherwise).
  2. reading order: top-to-bottom, left-to-right tiebreak.
  3. beats: split the strip into reading-order beats by whitespace-gutter
     detection (near-uniform background rows with no detections = scene breaks),
     falling back to bubble-cluster grouping inside any over-long beat.
  4. OCR: English text per beat's text boxes (PaddleOCR en), concatenated in
     reading order, split by detector class into `narration` (text_free) and
     `dialogue` (text_bubble), with near-duplicate OCR strings dropped as a
     backstop. No translation.
  5. write the contract + debug overlays.
"""

# --------------------------------------------------------------------------- #
# 0. Dependencies (pinned). Kaggle has torch + torchvision + CUDA preinstalled;
#    we only add the detector/OCR stack. Uncomment to run in a fresh notebook.
# --------------------------------------------------------------------------- #
# RT-DETR-v2 support in transformers landed in 4.49; timm provides the backbone.
# OCR uses the PaddlePaddle *CPU* wheel on purpose: the GPU wheel must match the
# notebook's exact CUDA build and frequently fails to install, which would block
# the whole run. Detection stays on GPU; CPU OCR is plenty for one strip's worth
# of bubbles. OCR is also a best-effort final pass (see main): if it fails, the
# detection/beats/crops/debug/panels.json outputs are already on disk.
#
#   !pip install -q \
#       transformers==4.49.0 timm==1.0.15 \
#       paddleocr==2.9.1 paddlepaddle==2.6.2 \
#       opencv-python-headless==4.10.0.84 pillow==10.4.0 numpy==1.26.4
#
# Alternative OCR (if paddle misbehaves): python-doctr - swap inside OcrEngine,
# it's the only place OCR is touched.
#   !pip install -q "python-doctr[torch]==0.9.0"

import json
import os
import sys
from glob import glob
from pathlib import Path

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont

# Webtoon strips are enormous; lift Pillow's decompression-bomb guard.
Image.MAX_IMAGE_PIXELS = None

# --------------------------------------------------------------------------- #
# 1. Config - everything tunable in one place.
# --------------------------------------------------------------------------- #
INPUT_DIR = Path(os.environ.get("INPUT_DIR", "/kaggle/input"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/kaggle/working/stage2_out"))

MODEL_ID = "ogkalu/comic-text-and-bubble-detector"
ID2LABEL = {0: "bubble", 1: "text_bubble", 2: "text_free"}
TEXT_LABELS = {"text_bubble", "text_free"}   # boxes that carry OCR-able text

# Detection / tiling
TILE_H = 1024          # vertical window height
TILE_OVERLAP = 256     # overlap between consecutive windows
DET_THRESHOLD = 0.35   # min confidence to keep a box
NMS_IOU = 0.5          # class-aware NMS IoU to merge window-edge duplicates
# Extra TEXT-box dedupe (text_bubble/text_free) run after NMS: overlapping tiles
# detect the same bubble's text twice, offset just enough to survive NMS_IOU.
TEXT_DEDUPE_IOU = 0.3          # lower IoU at which two same-class text boxes merge
TEXT_DEDUPE_CENTER_FRAC = 0.6  # ...or centers within this * the smaller box height

# Beat (whitespace-gutter) segmentation
GUTTER_ROW_STD = 8.0   # grayscale std below this => row is "uniform background"
MIN_GUTTER_PX = 24     # a run of >= this many gutter rows is a scene break
MAX_BEAT_H = 2200      # beats taller than this get the bubble-cluster fallback
CLUSTER_GAP_PX = 220   # vertical gap between bubble clusters that triggers a split

# Debug preview
PREVIEW_MAX_H = 2000

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


# --------------------------------------------------------------------------- #
# 2. Input discovery
# --------------------------------------------------------------------------- #
def find_strip() -> Path:
    """Locate the single stitched strip in INPUT_DIR (recursively).

    Prefers files named like `strip_*.png` (stage-1 output), excluding the
    small `*_preview.png`. Falls back to the largest PNG if naming differs.
    """
    candidates = [
        Path(p) for p in glob(str(INPUT_DIR / "**" / "strip_*.png"), recursive=True)
        if "_preview" not in Path(p).name
    ]
    if not candidates:
        candidates = [Path(p) for p in glob(str(INPUT_DIR / "**" / "*.png"), recursive=True)]
    if not candidates:
        sys.exit(
            f"No strip image found under {INPUT_DIR}. Upload the stage-1 output "
            f"(work/<chapter>/pages/strip_001.png) as a Kaggle dataset and set "
            f"INPUT_DIR to it."
        )
    # If several, take the tallest - that's the real full-res strip.
    strip = max(candidates, key=lambda p: Image.open(p).size[1])
    if len(candidates) > 1:
        print(f"  [warn] {len(candidates)} candidate strips; using tallest: {strip.name}")
    return strip


# --------------------------------------------------------------------------- #
# 3. Detection (tiled) + NMS
# --------------------------------------------------------------------------- #
def load_detector():
    from transformers import AutoImageProcessor, AutoModelForObjectDetection

    print(f"  loading detector {MODEL_ID} on {DEVICE} ...")
    processor = AutoImageProcessor.from_pretrained(MODEL_ID)
    model = AutoModelForObjectDetection.from_pretrained(MODEL_ID).to(DEVICE).eval()
    return processor, model


@torch.no_grad()
def detect_tiled(strip: Image.Image, processor, model):
    """Detect over overlapping vertical windows; return full-strip-coord boxes.

    Returns a list of dicts: {box:[x1,y1,x2,y2], score:float, label:str}.
    """
    W, H = strip.size
    step = max(1, TILE_H - TILE_OVERLAP)
    tops = list(range(0, max(1, H - TILE_OVERLAP), step))
    if tops and tops[-1] + TILE_H < H:
        tops.append(H - TILE_H)
    tops = [max(0, t) for t in tops]

    raw = []
    for i, top in enumerate(tops):
        bottom = min(top + TILE_H, H)
        tile = strip.crop((0, top, W, bottom))
        inputs = processor(images=tile, return_tensors="pt").to(DEVICE)
        outputs = model(**inputs)
        target = torch.tensor([[tile.height, tile.width]], device=DEVICE)
        result = processor.post_process_object_detection(
            outputs, target_sizes=target, threshold=DET_THRESHOLD
        )[0]
        n = 0
        for box, score, label in zip(result["boxes"], result["scores"], result["labels"]):
            x1, y1, x2, y2 = box.tolist()
            raw.append({
                "box": [x1, y1 + top, x2, y2 + top],   # map back to strip coords
                "score": float(score),
                "label": ID2LABEL.get(int(label), str(int(label))),
            })
            n += 1
        print(f"    window {i + 1}/{len(tops)} [y {top}:{bottom}] -> {n} boxes")

    merged = class_aware_nms(raw, NMS_IOU)
    print(f"  detected {len(merged)} boxes after NMS (from {len(raw)} raw)")
    merged = dedupe_text_boxes(merged)
    return merged


def class_aware_nms(dets, iou_thr):
    from torchvision.ops import nms

    if not dets:
        return []
    kept = []
    for label in {d["label"] for d in dets}:
        group = [d for d in dets if d["label"] == label]
        boxes = torch.tensor([d["box"] for d in group], dtype=torch.float32)
        scores = torch.tensor([d["score"] for d in group], dtype=torch.float32)
        idx = nms(boxes, scores, iou_thr).tolist()
        kept.extend(group[i] for i in idx)
    return kept


def _iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _is_text_dup(a, b):
    """True if two same-class text boxes are the same bubble seen twice."""
    if _iou(a, b) >= TEXT_DEDUPE_IOU:
        return True
    acx, acy = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
    bcx, bcy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    dist = ((acx - bcx) ** 2 + (acy - bcy) ** 2) ** 0.5
    min_h = min(a[3] - a[1], b[3] - b[1])
    return dist < TEXT_DEDUPE_CENTER_FRAC * max(1.0, min_h)


def dedupe_text_boxes(dets):
    """Aggressively drop near-duplicate TEXT boxes that class-aware NMS left.

    Overlapping tiles detect the same bubble's text twice; the two boxes are
    often offset just enough (IoU < NMS_IOU) that NMS keeps both, which doubles
    every beat's OCR. Here we re-test same-class text boxes with a lower IoU
    *and* a center-distance check, keeping the highest-scoring of each cluster.
    Non-text boxes (`bubble`) pass through untouched.
    """
    text = [d for d in dets if d["label"] in TEXT_LABELS]
    other = [d for d in dets if d["label"] not in TEXT_LABELS]
    kept = []
    for d in sorted(text, key=lambda x: x["score"], reverse=True):
        if any(d["label"] == k["label"] and _is_text_dup(d["box"], k["box"])
               for k in kept):
            continue
        kept.append(d)
    dropped = len(text) - len(kept)
    if dropped:
        print(f"  text dedupe: dropped {dropped} near-duplicate text box(es) "
              f"({len(text)} -> {len(kept)})")
    return other + kept


# --------------------------------------------------------------------------- #
# 4. Beat segmentation (whitespace gutters + bubble-cluster fallback)
# --------------------------------------------------------------------------- #
def segment_beats(strip: Image.Image, dets):
    """Return a list of (top, bottom) beat spans in reading order."""
    W, H = strip.size
    gray = np.asarray(strip.convert("L"), dtype=np.float32)
    row_std = gray.std(axis=1)                      # uniformity per row

    # A row is "content" if any detection covers it (bubbles/text must not be cut).
    content = np.zeros(H, dtype=bool)
    for d in dets:
        _, y1, _, y2 = d["box"]
        content[max(0, int(y1)):min(H, int(y2) + 1)] = True

    gutter = (row_std < GUTTER_ROW_STD) & (~content)

    # Split at the midpoint of every long gutter run.
    splits = []
    y = 0
    while y < H:
        if gutter[y]:
            start = y
            while y < H and gutter[y]:
                y += 1
            if y - start >= MIN_GUTTER_PX:
                splits.append((start + y) // 2)
        else:
            y += 1

    bounds = [0] + splits + [H]
    beats = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)
             if bounds[i + 1] - bounds[i] > 1]

    # Fallback: break any over-long beat at large vertical gaps between bubbles.
    refined = []
    for top, bottom in beats:
        if bottom - top <= MAX_BEAT_H:
            refined.append((top, bottom))
            continue
        refined.extend(split_by_bubble_clusters(top, bottom, dets))
    print(f"  segmented {len(refined)} beats "
          f"({len(splits)} gutter splits, {len(refined) - len(beats)} cluster splits)")
    return refined


def split_by_bubble_clusters(top, bottom, dets):
    """Split a tall gutter-less beat between vertically separated bubble clusters."""
    boxes = sorted(
        ([int(d["box"][1]), int(d["box"][3])] for d in dets
         if top <= (d["box"][1] + d["box"][3]) / 2 < bottom),
        key=lambda yy: yy[0],
    )
    if len(boxes) < 2:
        return [(top, bottom)]

    cuts = []
    prev_bottom = boxes[0][1]
    for y1, y2 in boxes[1:]:
        if y1 - prev_bottom > CLUSTER_GAP_PX:
            cuts.append((prev_bottom + y1) // 2)
        prev_bottom = max(prev_bottom, y2)

    bounds = [top] + cuts + [bottom]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)
            if bounds[i + 1] - bounds[i] > 1]


# --------------------------------------------------------------------------- #
# 5. OCR (English). Isolated so it can be swapped for doctr without touching
#    the rest of the pipeline.
# --------------------------------------------------------------------------- #
class OcrEngine:
    def __init__(self):
        from paddleocr import PaddleOCR
        print("  loading PaddleOCR (en, CPU) ...")
        # CPU on purpose - the paddle GPU wheel is fragile on Kaggle and OCR
        # volume here is tiny. Detection already used the GPU.
        self.ocr = PaddleOCR(
            use_angle_cls=True, lang="en", show_log=False, use_gpu=False,
        )

    def read(self, crop: Image.Image) -> str:
        arr = np.asarray(crop.convert("RGB"))
        if arr.shape[0] < 6 or arr.shape[1] < 6:
            return ""
        result = self.ocr.ocr(arr, cls=True)
        if not result or not result[0]:
            return ""
        lines = []
        for line in result[0]:
            # line == [box, (text, confidence)]
            try:
                lines.append(line[1][0])
            except (IndexError, TypeError):
                continue
        return " ".join(t.strip() for t in lines if t and t.strip())


def _norm_text(s: str) -> str:
    """Lowercase + collapse whitespace, for comparing OCR fragments."""
    return " ".join(s.lower().split())


def _join_dedup(parts) -> str:
    """Concatenate OCR fragments in order, dropping near-duplicate strings.

    A backstop for the box-level dedupe: if the same bubble still slipped
    through twice, its text would repeat (e.g. "MY NAME IS ZHAO QI MY NAME IS
    ZHAO QI"). Drops a fragment whose normalized text equals or is contained in
    one already kept; if a new fragment is a superset of a kept one, it replaces
    it (so the fuller OCR wins).
    """
    kept = []   # list of [normalized, original]
    for p in parts:
        n = _norm_text(p)
        if not n:
            continue
        dup = False
        for entry in kept:
            if n == entry[0] or n in entry[0]:
                dup = True
                break
            if entry[0] in n:                 # new fragment supersedes the kept one
                entry[0], entry[1] = n, p.strip()
                dup = True
                break
        if not dup:
            kept.append([n, p.strip()])
    return " ".join(entry[1] for entry in kept)


def ocr_beat(strip: Image.Image, span, dets, ocr: OcrEngine) -> dict:
    """OCR a beat's text boxes, split by detector class.

    Returns {"narration": str, "dialogue": str}: `narration` from text_free
    boxes (captions/SFX outside bubbles), `dialogue` from text_bubble boxes,
    each concatenated in reading order (T->B, L->R) with near-duplicate OCR
    strings dropped.
    """
    top, bottom = span
    out = {}
    for field, label in (("narration", "text_free"), ("dialogue", "text_bubble")):
        boxes = [
            d for d in dets
            if d["label"] == label and top <= (d["box"][1] + d["box"][3]) / 2 < bottom
        ]
        boxes.sort(key=lambda d: (round(d["box"][1] / 20), d["box"][0]))  # T->B, L->R
        parts = []
        for d in boxes:
            x1, y1, x2, y2 = (int(v) for v in d["box"])
            crop = strip.crop((max(0, x1), max(0, y1), x2, y2))
            txt = ocr.read(crop)
            if txt:
                parts.append(txt)
        out[field] = _join_dedup(parts)
    return out


# --------------------------------------------------------------------------- #
# 6. Debug overlay
# --------------------------------------------------------------------------- #
LABEL_COLORS = {"bubble": (0, 160, 255), "text_bubble": (0, 220, 0),
                "text_free": (255, 120, 0)}


def write_debug(strip: Image.Image, dets, beats, debug_dir: Path):
    debug_dir.mkdir(parents=True, exist_ok=True)
    canvas = strip.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)
    W, _ = strip.size

    for d in dets:
        x1, y1, x2, y2 = d["box"]
        draw.rectangle([x1, y1, x2, y2], outline=LABEL_COLORS.get(d["label"], (200, 0, 200)), width=3)

    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 64)
    except OSError:
        font = ImageFont.load_default()

    for i, (top, bottom) in enumerate(beats, start=1):
        draw.line([(0, top), (W, top)], fill=(255, 0, 0), width=4)
        draw.rectangle([8, top + 8, 8 + 120, top + 8 + 80], fill=(255, 0, 0))
        draw.text((20, top + 12), str(i), fill=(255, 255, 255), font=font)

    full = debug_dir / "strip_boxes.png"
    canvas.save(full)

    preview = canvas
    if canvas.height > PREVIEW_MAX_H:
        scale = PREVIEW_MAX_H / canvas.height
        preview = canvas.resize(
            (max(1, round(canvas.width * scale)), PREVIEW_MAX_H), Image.LANCZOS
        )
    preview.save(debug_dir / "preview.png")
    print(f"  wrote debug overlay -> {full} (+ preview.png)")


# --------------------------------------------------------------------------- #
# 7. Main
# --------------------------------------------------------------------------- #
def save_panels(panels):
    path = OUTPUT_DIR / "panels.json"
    path.write_text(
        json.dumps({"panels": panels}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def run_ocr_pass(strip, beats, dets, panels):
    """Best-effort OCR: fill narration + dialogue and re-save panels.json.

    Any import/install/runtime failure is caught and logged so the outputs
    already written (crops, debug overlay, panels.json with empty text fields)
    survive intact. Returns True if OCR ran, False if it was skipped.
    """
    try:
        ocr = OcrEngine()
    except Exception as e:  # noqa: BLE001 - missing wheel, CUDA, download, etc.
        print(f"  [warn] OCR unavailable ({type(e).__name__}: {e}); "
              f"leaving dialogue empty. Fix OCR deps and re-run to fill text.")
        return False

    by_order = {p["order"]: p for p in panels}
    filled = 0
    for order, (top, bottom) in enumerate(beats, start=1):
        try:
            text = ocr_beat(strip, (top, bottom), dets, ocr)
        except Exception as e:  # noqa: BLE001 - one bad crop shouldn't sink the run
            print(f"  [warn] OCR failed on beat {order} ({type(e).__name__}: {e})")
            continue
        by_order[order]["narration"] = text["narration"]
        by_order[order]["dialogue"] = text["dialogue"]
        if text["narration"] or text["dialogue"]:
            filled += 1
        print(f"    beat {order:>3} [y {top}:{bottom}]  "
              f"narration={text['narration'][:40]!r}  dialogue={text['dialogue'][:40]!r}")
        save_panels(panels)   # re-save incrementally so progress is never lost
    print(f"  OCR filled text for {filled}/{len(beats)} beats")
    return True


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    crops_dir = OUTPUT_DIR / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    print("[1/6] locating strip ...")
    strip_path = find_strip()
    strip = Image.open(strip_path).convert("RGB")
    print(f"  strip: {strip_path.name}  {strip.width}x{strip.height}")

    print("[2/6] loading detector ...")
    processor, model = load_detector()

    print("[3/6] detecting (tiled) ...")
    dets = detect_tiled(strip, processor, model)

    print("[4/6] segmenting beats + writing crops ...")
    beats = segment_beats(strip, dets)
    panels = []
    for order, (top, bottom) in enumerate(beats, start=1):
        crop = strip.crop((0, top, strip.width, bottom))
        crop_name = f"{order:04d}.png"               # globally unique
        crop.save(crops_dir / crop_name)
        panels.append({
            "order": order,
            "page": 1,                               # webtoon = single strip
            "bbox": [0, int(top), int(strip.width), int(bottom - top)],
            "narration": "",                         # text_free, filled by OCR below
            "dialogue": "",                          # text_bubble, filled by OCR below
            "speaker": None,
            "crop_file": f"crops/{crop_name}",
        })

    # Write the full contract + debug overlay BEFORE OCR, so a first run always
    # produces inspectable output even if OCR can't load.
    path = save_panels(panels)
    print(f"  wrote {path}  ({len(panels)} panels, dialogue empty)")

    print("[5/6] debug overlay ...")
    write_debug(strip, dets, beats, OUTPUT_DIR / "debug")

    print("[6/6] OCR pass (best-effort) ...")
    run_ocr_pass(strip, beats, dets, panels)

    print(f"\nDone. Output in {OUTPUT_DIR}/")
    print("Download stage2_out/ and extract it to work/<chapter>/_kaggle/ on the laptop.")


if __name__ == "__main__":
    main()

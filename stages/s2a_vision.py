"""Stage 2a - LOCAL vision tagging + CHARACTER CROP.  [REAL]
Runs on: your laptop (CPU). No Groq, no API, no Kaggle, no network at run time
(YOLO downloads its tiny weights once on first use, then runs fully local).

Stage 2 over-cuts the strip into candidate panels and reads their OCR text.
Selection (s2b) used to see ONLY that text, so it always picked speech-bubble
slices and never actually LOOKED at the art. This stage fixes that: it opens
each candidate crop, tags whether it shows a PERSON or a close-up FACE, and -
critically - CROPS the panel down to the character with the speech bubble trimmed
off, turning "face + bubble" panels into clean, usable frames.

Per candidate crop in `work/<chapter>/panels/`, with LOCAL CPU models only:
  - person detection via ultralytics YOLO (yolov8n - the tiny model). COCO class
    0 = person. Fills `has_person` + `person_count` and gives person bboxes.
  - face detection via OpenCV Haar cascade (ships with opencv, no download) for
    close-up faces. Fills `has_face` + `face_count` and gives face bboxes.
  - `white_fraction`: fraction of near-white/uniform pixels (a speech bubble or
    blank gutter is mostly white).
  - OCR text amount: taken from the `Panel.dialogue` stage 2 already filled.

It then CLASSIFIES each character panel and crops ONLY close-ups (`Panel.frame_kind`):
  - "closeup": a character large in frame, usually with a speech bubble / blank
    area to remove. It computes a CROP RECTANGLE around the person/face box,
    trims near-white bubble/blank bands off the margin (never cutting into the
    character), writes it to `work/<chapter>/panels_cropped/panel_NNN.png` and
    stores it on `Panel.crop_clean`. Downstream SHOWS it via `Panel.frame_image`.
  - "impact": a big, busy, dark/saturated art panel (a battle splash, full-bleed
    action), possibly with stylized sound-effect text drawn ON the art (NOT a
    white bubble). The whole composition is the value, so it is KEPT FULL - never
    cropped (cropping ruins it).
  - "full": anything else eligible (the conservative default) - kept full so we
    never over-crop. A close-up whose tight crop would be junk (tiny / mostly
    white / near-uniform blob) also falls back to full.
Impact/full panels set no `crop_clean`, so `Panel.frame_image` returns the full
panel. The impact thresholds (size / white / contrast / saturation) and the
close-up thresholds are tunable constants below.

A candidate is flagged `is_text_only` when it shows NO person and NO face and is
mostly white and/or carries a chunk of OCR text - i.e. a speech-bubble / caption
slice with no visible character. Selection NEVER keeps a text_only panel; it
folds that panel's dialogue into the nearest kept character frame instead.

Both detectors are best-effort / NON-FATAL: if ultralytics or opencv is missing
(or YOLO's weights can't be fetched) we warn once and leave the relevant flags
at their defaults (no crop) - the rest of the pipeline still runs, downstream
falls back to the raw crop, and s2b falls back to its OCR-only behaviour.

A human-readable `work/<chapter>/vision/vision.txt` lists every panel's tags +
crop for review. Owns the s2a fields only (see manifest). run(m) -> m, sets
m.stage = "vision" (between "panels" and "script"); never saves.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

# Allow running this file directly for the self-test; harmless when imported.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import chapter_work_dir
from manifest import Manifest

Image.MAX_IMAGE_PIXELS = None

# --------------------------------------------------------------------------- #
# Tunables - detection thresholds + the text_only heuristic.
# --------------------------------------------------------------------------- #
# YOLO confidence floor for counting a "person". Lower => more permissive.
PERSON_CONF = 0.30

# A pixel brighter than this (0-255 grayscale) counts as near-white/uniform.
WHITE_LEVEL = 235

# A candidate with no person/face is "text_only" when it is at least this white
# OR carries at least MIN_TEXT_LEN characters of OCR text.
WHITE_FRAC_TEXT = 0.55
MIN_TEXT_LEN = 12

# Size floors - the minimum detection size that makes a panel a usable character
# frame. These are LOWER than before on purpose: now that we CROP to the
# character and trim the bubble away, a bubble-heavy panel with a modest person
# in it still yields a clean frame, so it should count. The floor only exists to
# reject true garbage (a few stray pixels of a person), not bubble-heavy panels.
#
#   PERSON_MIN_AREA_FRAC: a person bbox must cover >= this fraction of the panel
#     AREA to count / be cropped to. Raise = stricter (only big figures).
#   FACE_MIN_WIDTH_FRAC: a face bbox width must be >= this fraction of the panel
#     WIDTH. Gated by width, not area, because webtoon panels are very TALL - a
#     real close-up face is a small fraction of a tall panel's AREA but spans a
#     good chunk of its WIDTH. Raise = stricter.
PERSON_MIN_AREA_FRAC = 0.12
FACE_MIN_WIDTH_FRAC = 0.07

# Cropping. CROP_MARGIN_FRAC pads the character box by this fraction of its size
# on each side (~10-15%) so the frame isn't cut tight to the skin. WHITE_TRIM_FRAC
# is how white a margin row/column must be to count as a blank/bubble band that
# gets trimmed off the crop edge (a speech bubble interior is mostly white). The
# trim never cuts into the character box itself.
CROP_MARGIN_FRAC = 0.12
WHITE_TRIM_FRAC = 0.80

# Minimum crop side (px). A small detected face can box a tiny region that would
# upscale to a blurry frame in the 1080p video; if a crop is smaller than this on
# a side, it is grown (centered, clipped to the panel) to keep usable resolution.
MIN_CROP_PX = 256

# --------------------------------------------------------------------------- #
# Close-up vs impact classification - CROP ONLY close-ups.
# --------------------------------------------------------------------------- #
# We only tighten-crop CLOSE-UP panels (a character large in frame, usually with
# a speech bubble / blank area to remove). IMPACT / action / splash panels - big,
# busy, dark-or-saturated art filling the frame, often with stylized sound-effect
# text drawn ON the art (NOT a white bubble) - are KEPT FULL: the whole
# composition is the value and cropping ruins them. Everything else eligible is
# also kept full (conservative default - never over-crop).
#
# IMPACT = large area AND not-mostly-white AND (high contrast OR high saturation).
#   IMPACT_MIN_AREA_PX:    panel must be at least this many pixels (a big panel).
#   IMPACT_MAX_WHITE:      reject if more white than this (a bubble panel, not art).
#   IMPACT_MIN_CONTRAST:   grayscale std at/above this = busy/high-contrast art.
#   IMPACT_MIN_SATURATION: mean HSV saturation at/above this = vivid art (e.g. a
#                          dark-red battle splash) even if contrast is moderate.
IMPACT_MIN_AREA_PX = 700_000
IMPACT_MAX_WHITE = 0.25
IMPACT_MIN_CONTRAST = 50.0
IMPACT_MIN_SATURATION = 60.0

# CLOSE-UP = (not impact) AND there is something to gain by cropping: either a
# meaningful white/blank/bubble area to remove, or the character box itself
# already dominates the frame (a true close-up).
#   CLOSEUP_MIN_WHITE:        white fraction at/above this => bubble/blank to trim.
#   CLOSEUP_MIN_SUBJECT_FRAC: character box covers >= this fraction of the panel.
CLOSEUP_MIN_WHITE = 0.30
CLOSEUP_MIN_SUBJECT_FRAC = 0.45

# Junk-crop rejection - never emit a tiny, meaningless crop. If the computed crop
# is mostly white or near-uniform (a blob), discard it and keep the full panel.
JUNK_MAX_WHITE = 0.85
JUNK_MIN_CONTRAST = 10.0

# Haar face detection params (passed to detectMultiScale).
FACE_SCALE_FACTOR = 1.1
FACE_MIN_NEIGHBORS = 5
# Ignore tiny false faces: a real close-up face spans a fair part of the crop.
FACE_MIN_SIDE_FRAC = 0.06


# --------------------------------------------------------------------------- #
# Detector loaders - both best-effort / non-fatal.
# --------------------------------------------------------------------------- #
def _load_yolo():
    """Return (name, detect_fn) where detect_fn(path)->[(bbox, area_frac), ...]
    (one entry per detected person: its pixel bbox (x1,y1,x2,y2) and bbox area as
    a fraction of the panel), or (None, None) if ultralytics/YOLO weights are
    unavailable (non-fatal)."""
    try:
        from ultralytics import YOLO
    except Exception as e:                              # noqa: BLE001
        print(f"  YOLO unavailable ({type(e).__name__}); person detection off "
              f"(pip install ultralytics)")
        return None, None
    try:
        model = YOLO("yolov8n.pt")                      # tiny model, CPU
    except Exception as e:                              # noqa: BLE001
        print(f"  YOLO weights load failed ({type(e).__name__}); person "
              f"detection off")
        return None, None

    def detect(path: str) -> list[tuple]:
        # Single-image, CPU, quiet. COCO class 0 == person.
        results = model.predict(path, device="cpu", conf=PERSON_CONF,
                                classes=[0], verbose=False)
        dets: list[tuple] = []
        for r in results:
            if r.boxes is None:
                continue
            h, w = r.orig_shape
            panel_area = float(h * w) or 1.0
            for box in r.boxes.xyxy.cpu().numpy():
                x1, y1, x2, y2 = box[:4]
                area = max(0.0, x2 - x1) * max(0.0, y2 - y1)
                bbox = (int(x1), int(y1), int(x2), int(y2))
                dets.append((bbox, area / panel_area))
        return dets

    return "yolov8n", detect


def _load_face():
    """Return (name, detect_fn) where detect_fn(pil_img)->[(bbox, width_frac), ...]
    (one entry per detected face: its pixel bbox (x1,y1,x2,y2) and bbox WIDTH as a
    fraction of the panel width), or (None, None) if opencv is unavailable
    (non-fatal)."""
    try:
        import cv2
    except Exception as e:                              # noqa: BLE001
        print(f"  OpenCV unavailable ({type(e).__name__}); face detection off "
              f"(pip install opencv-python)")
        return None, None
    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    cascade = cv2.CascadeClassifier(str(cascade_path))
    if cascade.empty():
        print("  Haar cascade failed to load; face detection off")
        return None, None

    def detect(img: Image.Image) -> list[tuple]:
        gray = np.asarray(img.convert("L"))
        h, w = gray.shape
        panel_w = float(w) or 1.0
        min_side = max(20, int(min(h, w) * FACE_MIN_SIDE_FRAC))
        faces = cascade.detectMultiScale(
            gray, scaleFactor=FACE_SCALE_FACTOR,
            minNeighbors=FACE_MIN_NEIGHBORS, minSize=(min_side, min_side),
        )
        return [((int(x), int(y), int(x + fw), int(y + fh)), float(fw) / panel_w)
                for (x, y, fw, fh) in faces]

    return "haar", detect


def _white_fraction(gray: np.ndarray) -> float:
    """Fraction of near-white/uniform pixels (bubble/blank heuristic)."""
    if gray.size == 0:
        return 1.0
    return float((gray >= WHITE_LEVEL).mean())


def _saturation_mean(img: Image.Image) -> float:
    """Mean HSV saturation (0-255). Vivid art (a dark-red splash) scores high."""
    hsv = np.asarray(img.convert("HSV"))
    if hsv.size == 0:
        return 0.0
    return float(hsv[:, :, 1].mean())


# --------------------------------------------------------------------------- #
# Close-up vs impact classification.
# --------------------------------------------------------------------------- #
def _is_impact(area_px: float, white_frac: float, contrast: float,
               saturation: float) -> bool:
    """A big, busy, NON-bubble art panel (battle splash, full-bleed action) whose
    whole composition is the value -> keep it FULL, never crop."""
    return (area_px >= IMPACT_MIN_AREA_PX
            and white_frac < IMPACT_MAX_WHITE
            and (contrast >= IMPACT_MIN_CONTRAST
                 or saturation >= IMPACT_MIN_SATURATION))


def _is_closeup(white_frac: float, subject_frac: float) -> bool:
    """A character close-up worth tightening: either there's a bubble/blank area
    to remove, or the character box already dominates the frame."""
    return (white_frac >= CLOSEUP_MIN_WHITE
            or subject_frac >= CLOSEUP_MIN_SUBJECT_FRAC)


def _crop_is_junk(crop_gray: np.ndarray) -> bool:
    """A crop that ended up tiny/low-content (mostly white or near-uniform blob);
    such crops are discarded in favour of the fuller panel."""
    if crop_gray.size == 0:
        return True
    white = float((crop_gray >= WHITE_LEVEL).mean())
    contrast = float(crop_gray.std())
    return white >= JUNK_MAX_WHITE or contrast < JUNK_MIN_CONTRAST


# --------------------------------------------------------------------------- #
# Character crop - box the person/face, drop the bubble.
# --------------------------------------------------------------------------- #
def _subject_box(face_boxes: list[tuple], person_boxes: list[tuple]) -> tuple:
    """The character rectangle to crop around. Prefer the PERSON box when one was
    detected: it bounds the whole figure INCLUDING the head, so a speech bubble
    sitting above the head falls outside it and is dropped while the full face is
    kept. Fall back to the face box only for face-only (close-up) panels where
    YOLO found no body. Boxes are (x1, y1, x2, y2)."""
    def union(boxes):
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    if person_boxes:
        return union(person_boxes)
    return union(face_boxes)


def _trim_white(white_mask: np.ndarray, crop: tuple, subject: tuple) -> tuple:
    """Walk inward from each crop edge toward the subject box, dropping rows/cols
    that are mostly near-white (a blank or speech-bubble band). Never crosses the
    subject box, so the character is always fully kept. `white_mask` is a bool
    (H, W) array. Returns the trimmed (x1, y1, x2, y2)."""
    cx1, cy1, cx2, cy2 = crop
    sx1, sy1, sx2, sy2 = subject

    def row_white(y, x1, x2):
        seg = white_mask[y, x1:x2]
        return float(seg.mean()) if seg.size else 1.0

    def col_white(x, y1, y2):
        seg = white_mask[y1:y2, x]
        return float(seg.mean()) if seg.size else 1.0

    while cy1 < sy1 and row_white(cy1, cx1, cx2) >= WHITE_TRIM_FRAC:
        cy1 += 1
    while cy2 > sy2 and row_white(cy2 - 1, cx1, cx2) >= WHITE_TRIM_FRAC:
        cy2 -= 1
    while cx1 < sx1 and col_white(cx1, cy1, cy2) >= WHITE_TRIM_FRAC:
        cx1 += 1
    while cx2 > sx2 and col_white(cx2 - 1, cy1, cy2) >= WHITE_TRIM_FRAC:
        cx2 -= 1
    return (cx1, cy1, cx2, cy2)


def _compute_crop(size: tuple, subject: tuple, white_mask: np.ndarray) -> tuple:
    """Pad the subject box by CROP_MARGIN_FRAC (clipped to the panel), then trim
    blank/bubble bands back off the margin. Returns (x1, y1, x2, y2)."""
    w, h = size
    sx1, sy1, sx2, sy2 = subject
    bw = max(1, sx2 - sx1)
    bh = max(1, sy2 - sy1)
    mx = int(CROP_MARGIN_FRAC * bw)
    my = int(CROP_MARGIN_FRAC * bh)
    crop = (max(0, sx1 - mx), max(0, sy1 - my),
            min(w, sx2 + mx), min(h, sy2 + my))
    return _trim_white(white_mask, crop, subject)


def _grow_to(lo: int, hi: int, target: int, limit: int) -> tuple:
    """Expand interval [lo, hi] to width `target` (capped at `limit`), centered
    and clipped into [0, limit]."""
    target = min(target, limit)
    extra = target - (hi - lo)
    if extra <= 0:
        return lo, hi
    lo -= extra // 2
    hi += extra - extra // 2
    if lo < 0:
        hi -= lo
        lo = 0
    if hi > limit:
        lo -= hi - limit
        hi = limit
    return max(0, lo), min(limit, hi)


def _enforce_min_size(box: tuple, size: tuple, min_px: int) -> tuple:
    """Grow a too-small crop so neither side is below `min_px` (so a tiny face
    box doesn't upscale to a blurry frame), clipped to the panel."""
    w, h = size
    x1, y1, x2, y2 = box
    x1, x2 = _grow_to(x1, x2, min_px, w)
    y1, y2 = _grow_to(y1, y2, min_px, h)
    return (x1, y1, x2, y2)


def _classify(has_person: bool, has_face: bool, white_frac: float,
              text_len: int) -> dict:
    """Turn the area-gated detections + heuristics into the manifest flags.

    `has_person`/`has_face` already reflect the area gates (a sliver of a person
    or a tiny face does NOT count). A panel with no MEANINGFUL character that is
    mostly white and/or carries text is a speech-bubble / caption slice."""
    is_text_only = (
        not has_person and not has_face
        and (white_frac >= WHITE_FRAC_TEXT or text_len >= MIN_TEXT_LEN)
    )
    return {
        "has_person": has_person,
        "has_face": has_face,
        "white_fraction": round(white_frac, 3),
        "is_text_only": is_text_only,
    }


# --------------------------------------------------------------------------- #
# Stage entry point
# --------------------------------------------------------------------------- #
def run(m: Manifest) -> Manifest:
    work = chapter_work_dir(m.chapter_id)
    vision_dir = work / "vision"
    vision_dir.mkdir(parents=True, exist_ok=True)
    cropped_dir = work / "panels_cropped"
    cropped_dir.mkdir(parents=True, exist_ok=True)

    panels = m.ordered_panels()
    if not panels:
        print("  no candidate panels to tag")
        m.stage = "vision"
        return m

    yolo_name, detect_person = _load_yolo()
    face_name, detect_face = _load_face()
    if yolo_name:
        print(f"  person detector: {yolo_name}")
    if face_name:
        print(f"  face detector: {face_name}")

    lines: list[str] = []
    n_person = n_face = n_text = 0
    n_eligible = n_closeup = n_impact = n_full = n_junk = 0
    for panel in panels:
        crop_path = Path(panel.crop) if panel.crop else None
        if not crop_path or not crop_path.is_file():
            # Nothing to look at - leave flags at defaults.
            panel.has_person = panel.has_face = panel.is_text_only = False
            panel.person_count = panel.face_count = 0
            panel.crop_clean = None
            panel.frame_kind = None
            continue

        img = Image.open(crop_path).convert("RGB")
        gray = np.asarray(img.convert("L"))
        # detect_person -> (bbox, area_frac); detect_face -> (bbox, width_frac).
        person_dets = detect_person(str(crop_path)) if detect_person else []
        face_dets = detect_face(img) if detect_face else []
        # Size floor: ignore detections too small to make a usable cropped frame.
        big_person_boxes = [b for (b, f) in person_dets if f >= PERSON_MIN_AREA_FRAC]
        big_face_boxes = [b for (b, f) in face_dets if f >= FACE_MIN_WIDTH_FRAC]
        white_frac = _white_fraction(gray)
        text_len = len((panel.dialogue or "").strip())

        flags = _classify(bool(big_person_boxes), bool(big_face_boxes),
                          white_frac, text_len)
        panel.has_person = flags["has_person"]
        panel.has_face = flags["has_face"]
        panel.person_count = len(big_person_boxes)
        panel.face_count = len(big_face_boxes)
        panel.white_fraction = flags["white_fraction"]
        panel.is_text_only = flags["is_text_only"]

        n_person += panel.has_person
        n_face += panel.has_face
        n_text += panel.is_text_only

        # Classify each character panel: CROP ONLY close-ups; keep impact/action
        # splashes (and anything else) FULL so we never destroy a composition.
        crop_note = "-"
        panel.crop_clean = None
        panel.frame_kind = None
        if panel.has_person or panel.has_face:
            n_eligible += 1
            area_px = float(img.size[0] * img.size[1]) or 1.0
            contrast = float(gray.std())
            saturation = _saturation_mean(img)
            subject = _subject_box(big_face_boxes, big_person_boxes)
            sx1, sy1, sx2, sy2 = subject
            subject_frac = max(0, sx2 - sx1) * max(0, sy2 - sy1) / area_px

            if _is_impact(area_px, white_frac, contrast, saturation):
                panel.frame_kind = "impact"             # keep FULL (frame_image -> crop)
                n_impact += 1
                crop_note = "FULL(impact)"
            elif _is_closeup(white_frac, subject_frac):
                white_mask = gray >= WHITE_LEVEL
                box = _compute_crop(img.size, subject, white_mask)
                box = _enforce_min_size(box, img.size, MIN_CROP_PX)
                x1, y1, x2, y2 = box
                crop_gray = gray[y1:y2, x1:x2]
                if x2 - x1 >= 2 and y2 - y1 >= 2 and not _crop_is_junk(crop_gray):
                    out_path = cropped_dir / crop_path.name
                    img.crop(box).save(out_path)
                    panel.crop_clean = str(out_path)
                    panel.frame_kind = "closeup"
                    n_closeup += 1
                    kept_frac = (x2 - x1) * (y2 - y1) / area_px
                    crop_note = f"closeup crop={x2 - x1}x{y2 - y1}({kept_frac:.2f})"
                else:
                    panel.frame_kind = "full"           # junk crop -> keep full
                    n_junk += 1
                    crop_note = "FULL(junk-crop rejected)"
            else:
                panel.frame_kind = "full"               # not a close-up -> keep full
                n_full += 1
                crop_note = "FULL(not closeup)"

        tag = ("IMPACT" if panel.frame_kind == "impact" else
               "FACE" if panel.has_face else
               "PERSON" if panel.has_person else
               "TEXT" if panel.is_text_only else "other")
        max_p = max((f for (_b, f) in person_dets), default=0.0)
        max_f = max((f for (_b, f) in face_dets), default=0.0)
        contrast_note = float(gray.std())
        lines.append(
            f"[{panel.order:03d}] {tag:6s} person={len(big_person_boxes)} "
            f"face={len(big_face_boxes)} pArea={max_p:.2f} fWidth={max_f:.2f} "
            f"white={white_frac:.2f} contrast={contrast_note:.0f} text={text_len} "
            f"{crop_note}"
        )

    (vision_dir / "vision.txt").write_text("\n".join(lines) + "\n",
                                            encoding="utf-8")
    print(f"  tagged {len(panels)} panels: {n_face} with faces, "
          f"{n_person} with people, {n_text} text-only -> {vision_dir}")
    print(f"  eligible character frames: {n_eligible}; cropped {n_closeup} "
          f"close-ups; kept {n_impact} impact + {n_full + n_junk} other panels "
          f"FULL ({n_junk} were junk-crop rejected) -> {cropped_dir}")
    m.stage = "vision"
    return m


# --------------------------------------------------------------------------- #
# Self-test: the classifier logic (no models needed). Run:
#   python stages/s2a_vision.py
# --------------------------------------------------------------------------- #
def _self_test() -> None:
    # A clear close-up face (area gate already passed) -> has_face, not text_only.
    f = _classify(has_person=True, has_face=True, white_frac=0.2, text_len=0)
    assert f["has_face"] and f["has_person"] and not f["is_text_only"], f

    # Full body, no face -> has_person, not text_only.
    f = _classify(has_person=True, has_face=False, white_frac=0.1, text_len=30)
    assert f["has_person"] and not f["has_face"] and not f["is_text_only"], f

    # Sliver of head on a mostly-white bubble fails the area gate, so it arrives
    # here as no-person/no-face + white -> text_only (the case we want to drop).
    f = _classify(has_person=False, has_face=False, white_frac=0.8, text_len=40)
    assert f["is_text_only"] and not f["has_person"] and not f["has_face"], f

    # White slice, no text, no character -> still text_only (blank/bubble).
    f = _classify(has_person=False, has_face=False, white_frac=0.7, text_len=0)
    assert f["is_text_only"], f

    # Dark scenery, no character, no text, not white -> NOT text_only (and not
    # a character frame either: selection simply won't consider it).
    f = _classify(has_person=False, has_face=False, white_frac=0.1, text_len=2)
    assert not f["is_text_only"] and not f["has_person"], f

    # Subject box: when a person is detected, crop to the PERSON box (whole figure
    # incl. head; a bubble above the head is outside it), ignoring the face box.
    sub = _subject_box(face_boxes=[(40, 30, 60, 50)],
                       person_boxes=[(35, 25, 70, 90)])
    assert sub == (35, 25, 70, 90), sub
    # Face-only panel (no body) -> crop to the face box.
    assert _subject_box([(40, 30, 60, 50)], []) == (40, 30, 60, 50)

    # Crop trimming: a white bubble band above the subject is trimmed off the
    # margin, down to (never into) the subject top; non-white sides keep margin.
    white_mask = np.zeros((100, 100), dtype=bool)
    white_mask[:30, :] = True                            # bubble/blank band on top
    subject = (20, 30, 80, 80)
    crop = _compute_crop((100, 100), subject, white_mask)
    assert crop == (13, 30, 87, 86), crop                # top trimmed to subject top

    # Min-size guard: a tiny 100x100 face crop in a big panel grows to >=256/side.
    grown = _enforce_min_size((400, 400, 500, 500), (2000, 2000), 256)
    gw, gh = grown[2] - grown[0], grown[3] - grown[1]
    assert gw == 256 and gh == 256, grown
    # A crop already larger than the floor is left alone.
    assert _enforce_min_size((0, 0, 600, 800), (2000, 2000), 256) == (0, 0, 600, 800)
    # Growth clips to the panel when the panel is smaller than the floor.
    assert _enforce_min_size((40, 40, 60, 60), (100, 100), 256) == (0, 0, 100, 100)

    # Impact classification: big, low-white, vivid/high-contrast art = impact.
    assert _is_impact(area_px=1_300_000, white_frac=0.09, contrast=70,
                      saturation=140)                    # dark-red battle splash
    assert _is_impact(area_px=900_000, white_frac=0.10, contrast=20,
                      saturation=120)                    # vivid even if low contrast
    # A bubble-heavy close-up panel is NOT impact (too white), even if large.
    assert not _is_impact(area_px=1_300_000, white_frac=0.64, contrast=70,
                          saturation=30)
    # A small busy panel is NOT impact (not large enough).
    assert not _is_impact(area_px=300_000, white_frac=0.10, contrast=70,
                          saturation=120)

    # Close-up: enough white to trim, or the subject dominates the frame.
    assert _is_closeup(white_frac=0.64, subject_frac=0.20)   # bubble to remove
    assert _is_closeup(white_frac=0.10, subject_frac=0.55)   # subject dominates
    assert not _is_closeup(white_frac=0.10, subject_frac=0.20)

    # Junk-crop rejection: mostly-white or near-uniform crops are junk.
    assert _crop_is_junk(np.full((50, 50), 250, dtype=np.uint8))   # all white
    assert _crop_is_junk(np.full((50, 50), 40, dtype=np.uint8))    # flat/uniform
    assert not _crop_is_junk((np.arange(2500).reshape(50, 50) % 256).astype(np.uint8))

    print("s2a self-test OK")


if __name__ == "__main__":
    _self_test()

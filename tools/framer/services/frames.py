"""Cutting a line's frame out of the strip, with text blurred, and exporting a chapter's frames."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

import pacing
import projects
from atomic_io import atomic_write_json, clear_directory
from config import chapter_work_dir
from manifest import Manifest, Page, Panel
from services.chapter_files import _framer_dir, _strip_path, ensure_dir
from services.common import Refused
from services.editor_state import manifest_panels


# Blur for HIDING text. The effect strength scales with the blurred region's
# smaller side (not a fixed radius), so even big speech bubbles are destroyed.
# The SAME numbers drive the live preview and the exported frame (WYSIWYG).
BLUR_STRENGTH = 0.30      # gaussian radius / mosaic block size = this * minside


DEFAULT_BLUR_STYLE = "m"  # "m" = pixelate/mosaic (most unreadable), "g" = heavy gaussian


def _frame_suffix(j: int) -> str:
    """Letter suffix for the j-th (0-based) frame of a line: a, b, ... z, aa, ...

    Keeps per-line frame ORDER lexicographically sortable in the filename
    (frame_001a.png < frame_001b.png), so reading order survives on disk.
    """
    s = ""
    while True:
        s = chr(ord("a") + j % 26) + s
        j = j // 26 - 1
        if j < 0:
            return s


def _region_box_style(region):
    """A blur region is [x,y,w,h] or [x,y,w,h,style]; return (x,y,w,h,style)."""
    x, y, w, h = (int(round(float(v))) for v in region[:4])
    style = region[4] if len(region) > 4 else DEFAULT_BLUR_STYLE
    return x, y, w, h, style


def blur_region(img: Image.Image, lx: int, ly: int, rx: int, ry: int, style: str) -> None:
    """Destroy text in img[lx:rx, ly:ry] IN PLACE, intensity scaled to the region
    size so big bubbles are hit as hard as small ones.

    style "g" = heavy gaussian (radius = BLUR_STRENGTH * minside); anything else =
    mosaic/pixelate (downscale then nearest-neighbour upscale), which reduces the
    region to a few blocks and is the most reliable way to make text unreadable.
    A trailing "o" ("mo", "go") makes the region an ellipse inscribed in its box instead
    of the full rectangle. Shared by BOTH the export and the live preview so they look identical.
    """
    w, h = rx - lx, ry - ly
    if w <= 0 or h <= 0:
        return
    oval = style.endswith("o")
    style = style.rstrip("o")
    minside = min(w, h)
    patch = img.crop((lx, ly, rx, ry))
    if style == "g":
        blurred = patch.filter(ImageFilter.GaussianBlur(max(8.0, BLUR_STRENGTH * minside)))
    else:  # mosaic / pixelate (default)
        block = max(3, int(round(BLUR_STRENGTH * minside)))
        sw, sh = max(1, w // block), max(1, h // block)
        blurred = patch.resize((sw, sh), Image.BILINEAR).resize((w, h), Image.NEAREST)
    if oval:
        # anti-aliased ellipse: drawn at 4x, shrunk back
        mask = Image.new("L", (w * 4, h * 4), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, w * 4 - 1, h * 4 - 1), fill=255)
        blurred = Image.composite(blurred, patch, mask.resize((w, h), Image.LANCZOS))
    img.paste(blurred, (lx, ly))


def _crop_frame(strip: Image.Image, entry: dict, out_path: Path, blur: bool) -> None:
    """Crop one frame from the full-res strip, then flip it and, when `blur` is on, blur its regions.

    `entry` carries this frame's bbox + blur regions in TRUE strip pixels and a
    flip flag. Blur regions are translated into crop-local coords and blurred in
    place; the flip is applied last so on-screen left/right matches what the user toggled.
    """
    x, y, w, h = (int(round(v)) for v in entry["bbox"])
    x = max(0, min(x, strip.width - 1))
    y = max(0, min(y, strip.height - 1))
    w = max(1, min(w, strip.width - x))
    h = max(1, min(h, strip.height - y))
    crop = strip.crop((x, y, x + w, y + h))

    for region in (entry.get("blur") or []) if blur else []:
        bx, by, bw, bh, style = _region_box_style(region)
        # strip coords -> crop-local coords
        lx, ly = bx - x, by - y
        rx2, ry2 = lx + bw, ly + bh
        lx, ly = max(0, lx), max(0, ly)
        rx2, ry2 = min(crop.width, rx2), min(crop.height, ry2)
        if rx2 <= lx or ry2 <= ly:
            continue  # blur region doesn't overlap this crop
        blur_region(crop, lx, ly, rx2, ry2, style)

    if entry.get("flip"):
        crop = crop.transpose(Image.FLIP_LEFT_RIGHT)

    crop.save(str(out_path))


def export_chapter(chapter_id: str, entries: list[dict], slug: str | None, ch_no: str | None) -> dict:
    """Render every frame of `entries` and write the chapter's mapping.json and manifest."""
    if not entries:
        raise Refused("No entries to export.")
    if not any(f.get("bbox") for entry in entries for f in entry.get("frames") or []):
        raise Refused("No line had a frame bound. Draw a box first.")

    strip_path = _strip_path(chapter_id)
    if not strip_path.exists():
        raise Refused(f"No strip for {chapter_id}; load a PDF first.")
    strip = Image.open(strip_path).convert("RGB")
    blur = bool(slug) and projects.load(slug)["settings"]["framing"]["blur_text"]

    # The old frames and the narration voiced for the old lines go before the new frames are written.
    frames_dir = chapter_work_dir(chapter_id) / "frames"
    clear_directory(frames_dir)
    clear_directory(chapter_work_dir(chapter_id) / "audio")

    mapping = []
    beats = []
    total_frames = 0
    for i, entry in enumerate(entries, start=1):
        frames = [f for f in (entry.get("frames") or []) if f.get("bbox")]
        if not frames:
            continue  # a line with no frames bound yet - skip it

        line_text = (entry.get("line_text") or "").strip()
        duration_s = float(entry.get("duration_s") or 4.0)
        mode = "together" if entry.get("mode") == "together" else "sequence"
        pace = entry.get("pace") if entry.get("pace") in pacing.PACES else None

        out_frames = []
        for j, fr in enumerate(frames):
            frame_name = f"frame_{i:03d}{_frame_suffix(j)}.png"
            frame_path = frames_dir / frame_name
            _crop_frame(strip, fr, frame_path, blur)
            total_frames += 1
            out_frames.append({
                "file": frame_name,
                "image": str(frame_path),
                "flip": bool(fr.get("flip")),
                "blur_regions": [list(_region_box_style(r)) for r in (fr.get("blur") or [])] if blur else [],
                "bbox": [int(round(v)) for v in fr["bbox"]],
            })

        mapping.append({
            "order": i,
            "text": line_text,
            "duration_s": duration_s,
            "pace": pace,
            "mode": mode,
            "frames": out_frames,
        })
        # One beat per LINE (one narration -> one audio). The visible image is the first frame.
        beats.append(Panel(
            id=f"L{i:03d}",
            bbox=[float(v) for v in frames[0]["bbox"]],
            order=i,
            crop=out_frames[0]["image"],
            script_line=line_text,
            emotion=pace,
            duration_s=duration_s,
        ))

    framer_dir = ensure_dir(_framer_dir(chapter_id))
    mapping_path = framer_dir / "mapping.json"
    # Atomic + backed up (see atomic_io.py) - this is the finished boxing/script
    # work (line -> frame bindings), the exact thing that must never be lost
    # to a half-written file.
    atomic_write_json(mapping_path, {"chapter_id": chapter_id, "lines": mapping})

    # A manifest the orchestrator can resume from. stage="frames" means the next
    # stages to run are audio (s6) then video (s7).
    m = Manifest(
        chapter_id=chapter_id,
        source_pdf=str(framer_dir / "_upload.pdf"),
        work_dir=str(chapter_work_dir(chapter_id)),
        doc_type="webtoon",
        stage="frames",
        pages=[Page(index=1, image=str(strip_path), panels=manifest_panels(chapter_id))],
        beats=beats,
    )
    m.save()

    if slug and ch_no:
        projects.set_status(slug, ch_no, "boxed")
        projects.write_media_snapshot(slug, ch_no)

    return {
        "chapter_id": chapter_id,
        "lines_written": len(beats),
        "frames_written": total_frames,
        "frames_dir": str(frames_dir),
        "mapping_path": str(mapping_path),
        "manifest_path": str(m.path),
        "next_command": f"python orchestrator.py --chapter {chapter_id} --from audio",
    }

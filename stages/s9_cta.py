"""Stage 9 (add-on) - the call-to-action clip: a short 16:9 video asking viewers to like, subscribe and comment.

Like the Shorts stage it is outside STAGE_ORDER and the orchestrator and leaves `m.stage` alone. It reads
work/<chapter>/cta/plan.json (written by the framer's cta.py: the lines to say, each with the frames to show,
frames without any dialogue), voices the lines with the chapter's own voice (`s6_tts.voice`, into
work/<chapter>/cta/audio; the chapter's narration is untouched) and renders them with stage 7 under the
chapter's media settings (same voice, watermark and background as the chapter videos). The video is always
1080p: a project set to a smaller quality renders this one at "standard", so it still matches the chapters
and a merge can copy it in without re-encoding.

Output: output/<chapter>/cta/cta.mp4

Run from the project root:  python -m stages.s9_cta --chapter <id>
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from config import CTA_FILE, CTA_FOLDER, CTA_PLAN, chapter_output_dir, chapter_work_dir
from manifest import Manifest, Panel
from stages import s6_tts as s6
from stages import s7_assemble as s7

FRAME_SECONDS = 2.6    # a frame stays on screen about this long
MIN_HEIGHT = 1080


def beats_of(plan: dict) -> tuple[list[Panel], dict[int, dict]]:
    """The lines of `plan` as beats, and the mapping that gives each its frames (missing files dropped)."""
    panels, mapping = [], {}
    for k, line in enumerate(plan["lines"], start=1):
        frames = [f for f in line.get("frames", []) if Path(f).is_file()]
        panels.append(Panel(id=f"cta_{k:02d}", bbox=[0, 0, 0, 0], order=k, crop=frames[0] if frames else None,
                            emotion=line.get("pace"), script_line=line["text"]))
        mapping[k] = {"order": k, "mode": "sequence", "frames": [{"image": f} for f in frames]}
    return panels, mapping


def fit_frames(panels: list[Panel], mapping: dict[int, dict]) -> None:
    """Once voiced: show each line only as many of its frames as keep one on screen about FRAME_SECONDS."""
    for panel in panels:
        frames = mapping[panel.order]["frames"]
        want = max(1, round(float(panel.duration_s or 0) / FRAME_SECONDS))
        if len(frames) > want:
            frames = [frames[round(i * (len(frames) - 1) / max(1, want - 1))] for i in range(want)] if want > 1 else frames[:1]
        mapping[panel.order]["frames"] = frames
        if frames:
            panel.crop = frames[0]["image"]


def full_hd(settings: dict) -> dict:
    """`settings` with the quality raised to a 1080p preset when it is a smaller one."""
    name, preset = s7._resolve_quality(settings)
    return settings if preset["height"] >= MIN_HEIGHT else {**settings, "quality": "standard"}


def run(m: Manifest) -> Manifest:
    work = chapter_work_dir(m.chapter_id) / CTA_FOLDER
    plan = json.loads((work / CTA_PLAN).read_text(encoding="utf-8"))
    panels, mapping = beats_of(plan)
    if not panels:
        raise SystemExit("The call-to-action plan has no lines.")
    audio = work / "audio"
    shutil.rmtree(audio, ignore_errors=True)
    s6.voice(Manifest(chapter_id=m.chapter_id, source_pdf="", work_dir=str(work), beats=panels), audio)
    fit_frames(panels, mapping)

    settings = full_hd(s7.load_media_settings(m.chapter_id))
    render = s7.prepare_render(panels, mapping, settings, s7.LANDSCAPE)
    out_dir = chapter_output_dir(m.chapter_id) / CTA_FOLDER
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    s7.execute_render(render, out_dir / CTA_FILE)
    (out_dir / "cta.json").write_text(json.dumps({"duration_s": round(render.total, 1), "size": list(render.out_size)}),
                                      encoding="utf-8")
    return m


def main() -> None:
    ap = argparse.ArgumentParser(description="Render the call-to-action clip of a project from a chapter's cta plan")
    ap.add_argument("--chapter", required=True)
    run(Manifest.load(chapter_work_dir(ap.parse_args().chapter)))


if __name__ == "__main__":
    main()

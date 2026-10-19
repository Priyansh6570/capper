"""Stage 8 (add-on) - Shorts: vertical 9:16 videos of the segments the Shorts pipeline picked.

It is not part of the linear stage chain (STAGE_ORDER, orchestrator.py): it runs on a chapter that
already has its narration, frames and video, and leaves `m.stage` alone. It reads
work/<chapter>/shorts/plan.json, written by the framer's find step, and renders the chapter's own
frames and narration lines `first_line`..`last_line` of every planned segment with stage 7's renderer
on a portrait canvas, under the same media settings as the main video (the project's `shorts` settings
choose the layout, `frame` or `cover`, and may replace the watermark and background). Nothing is voiced again.

Output: output/<chapter>/shorts/short_NN.mp4 and shorts.json, the plan's entries plus each file (rewritten after
every short).

Run from the project root:  python -m stages.s8_shorts --chapter <id>
"""
from __future__ import annotations

import argparse
import json
import shutil

from config import SHORTS_FOLDER, SHORTS_INDEX, SHORTS_PLAN, chapter_output_dir, chapter_work_dir
from manifest import Manifest
from stages import s7_assemble as s7


def run(m: Manifest) -> Manifest:
    plan = json.loads((chapter_work_dir(m.chapter_id) / SHORTS_FOLDER / SHORTS_PLAN).read_text(encoding="utf-8"))
    out_dir = chapter_output_dir(m.chapter_id) / SHORTS_FOLDER
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)

    beats = {b.order: b for b in m.kept_panels()}
    mapping = s7.load_mapping(m.chapter_id)
    settings = s7.load_media_settings(m.chapter_id)
    shorts_settings = settings.get("shorts") or {}
    settings = {**settings, **(shorts_settings.get("look") or {})}
    cover = shorts_settings.get("layout") == "cover"
    rendered = []
    index = out_dir / SHORTS_INDEX
    for number, short in enumerate(plan["shorts"], start=1):
        print(f"shorts {number}/{len(plan['shorts'])}: {short['title']}", flush=True)
        segment = [beats[order] for order in sorted(beats) if short["first_line"] <= order <= short["last_line"]]
        render = s7.prepare_render(segment, mapping, settings, s7.PORTRAIT, cover)
        out = out_dir / f"short_{short['id']:02d}.mp4"
        s7.execute_render(render, out)
        rendered.append({**short, "file": out.name, "size": out.stat().st_size, "duration_s": round(render.total, 1)})
        index.write_text(  # after every short, so the app can show each one as soon as it is ready
            json.dumps({"chapter_id": m.chapter_id, "shorts": rendered}, indent=2, ensure_ascii=False), encoding="utf-8")
    return m


def main() -> None:
    ap = argparse.ArgumentParser(description="Render the planned shorts of a chapter")
    ap.add_argument("--chapter", required=True)
    run(Manifest.load(chapter_work_dir(ap.parse_args().chapter)))


if __name__ == "__main__":
    main()

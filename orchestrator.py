"""Resumable runner for the stages after the editor's export: narration (s6), then video (s7).

The export leaves the chapter's manifest at the "frames" marker. Each stage reads the manifest and the
files on disk, writes its files and advances the marker, and the manifest is saved after every stage,
so a crash just means running again: completed stages are skipped. The app starts this as a subprocess.

Usage (run from the project root):
    python orchestrator.py --chapter <id>                          # run whatever is left
    python orchestrator.py --chapter <id> --from video             # re-run from a given stage onward
    python orchestrator.py --chapter <id> --from audio --to audio  # regenerate ONLY the narration,
        # e.g. after changing the voice sample or speed, without touching the existing video
"""
from __future__ import annotations

import argparse
import importlib

from config import chapter_work_dir
from manifest import Manifest, STAGE_ORDER

# (marker this stage produces, module name under stages/). Order must match manifest.STAGE_ORDER.
# Modules are imported lazily, right before the stage runs, so launching a later stage does not load
# the heavy dependencies of an earlier one.
STAGES = [
    ("audio", "s6_tts"),
    ("video", "s7_assemble"),
]


def run(chapter_id: str, from_stage: str | None = None, to_stage: str | None = None) -> None:
    work = chapter_work_dir(chapter_id)
    if not (work / "manifest.json").exists():
        raise SystemExit(f"No manifest for {chapter_id} in {work}. Export the chapter from the editor first.")
    m = Manifest.load(work)
    if from_stage:
        m.stage = STAGE_ORDER[STAGE_ORDER.index(from_stage) - 1]

    stop_at = STAGE_ORDER.index(to_stage) if to_stage else None
    print(f"Chapter {chapter_id} - resuming after marker: {m.stage}")
    for produces, module_name in STAGES:
        if STAGE_ORDER.index(m.stage) >= STAGE_ORDER.index(produces):
            print(f"[skip] {produces}")
            continue
        module = importlib.import_module(f"stages.{module_name}")
        print(f"[run ] {module_name} -> {produces}")
        m = module.run(m)
        m.save()
        if stop_at is not None and STAGE_ORDER.index(produces) >= stop_at:
            print(f"\nStopped after stage {produces!r} (--to {to_stage}).")
            return
    print(f"\nDone. Final stage: {m.stage}")


def main() -> None:
    stage_names = [produces for produces, _ in STAGES]
    ap = argparse.ArgumentParser(description="Narration and video runner")
    ap.add_argument("--chapter", required=True)
    ap.add_argument("--from", dest="from_stage", default=None, choices=stage_names)
    ap.add_argument("--to", dest="to_stage", default=None, choices=stage_names,
                    help="stop after this stage (e.g. --from audio --to audio regenerates only "
                         "the narration, without re-rendering the video)")
    args = ap.parse_args()
    run(args.chapter, args.from_stage, args.to_stage)


if __name__ == "__main__":
    main()

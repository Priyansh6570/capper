"""Resumable pipeline runner.

Each stage reads the manifest + files on disk, does its work, writes files, and
advances manifest.stage. Progress is checkpointed after every stage, so a crash
or a Kaggle session timeout just means re-running - completed stages are
skipped. This file-based isolation keeps each stage independently testable and
easy to swap.

Usage (run from the project root):
    python make_fixture.py                 # create inputs/ch_001.pdf to test with
    python orchestrator.py                 # run ch_001, resuming where it left off
    python orchestrator.py --chapter ch_002
    python orchestrator.py --from script   # re-run from a given stage onward
    python orchestrator.py --force         # re-run everything from scratch
"""
from __future__ import annotations

import argparse

from config import INPUTS_DIR, chapter_work_dir
from manifest import Manifest, STAGE_ORDER
from stages import (s1_pdf_to_pages, s2_detect_panels, s2a_vision, s2b_select,
                    s3_context, s4_inpaint, s5_script, s6_tts, s7_assemble)

# (marker this stage produces, module). Order must match manifest.STAGE_ORDER.
# STORY-FIRST: s5 writes the full script (STAGE A) BEFORE s2b matches frames to
# its segments (STAGE B); context/audio/video then consume the matched beats.
STAGES = [
    ("pages",   s1_pdf_to_pages),
    ("panels",  s2_detect_panels),
    ("vision",  s2a_vision),
    ("script",  s5_script),     # STAGE A - story-first full script -> segments
    ("match",   s2b_select),    # STAGE B - match one frame to each segment
    ("context", s3_context),
    ("clean",   s4_inpaint),
    ("audio",   s6_tts),
    ("video",   s7_assemble),
]


def prompt_doc_type() -> str:
    """Ask once, interactively, when creating a new chapter and no --type given."""
    while True:
        choice = input(
            "PDF type? [1] page-based manga  [2] vertical webtoon: "
        ).strip()
        if choice == "1":
            return "manga"
        if choice == "2":
            return "webtoon"
        print("  please enter 1 or 2.")


def load_or_init(chapter_id: str, doc_type: str | None = None) -> Manifest:
    work = chapter_work_dir(chapter_id)
    if (work / "manifest.json").exists():
        # doc_type is decided once at creation and persisted; ignore --type now.
        return Manifest.load(work)

    # Image-folder chapter: slices pre-downloaded to work/<chapter>/input/. No
    # PDF needed; such a chapter is inherently a vertical strip -> webtoon.
    images = s1_pdf_to_pages.list_input_images(chapter_id)
    if images:
        m = Manifest(
            chapter_id=chapter_id,
            source_pdf="",
            work_dir=str(work),
            doc_type="webtoon",
        )
        m.save()
        return m

    pdf = INPUTS_DIR / f"{chapter_id}.pdf"
    if not pdf.exists():
        raise SystemExit(
            f"No manifest, no images at {s1_pdf_to_pages.chapter_input_dir(chapter_id)}, "
            f"and no PDF at {pdf}. Run: python make_fixture.py"
        )
    resolved = doc_type or prompt_doc_type()
    m = Manifest(
        chapter_id=chapter_id,
        source_pdf=str(pdf),
        work_dir=str(work),
        doc_type=resolved,
    )
    m.save()
    return m


def run(chapter_id: str, from_stage: str | None, force: bool,
        doc_type: str | None = None) -> None:
    m = load_or_init(chapter_id, doc_type)
    if m.stage not in STAGE_ORDER:
        # A manifest from the old (pre-story-first) pipeline used a marker that no
        # longer exists (e.g. "selection"); restart it cleanly.
        print(f"  manifest marker {m.stage!r} is from an older pipeline; "
              f"re-running from the start")
        m.stage = "init"
    if force:
        m.stage = "init"
    elif from_stage:
        m.stage = STAGE_ORDER[STAGE_ORDER.index(from_stage) - 1]

    print(f"Chapter {chapter_id} - resuming after marker: {m.stage}")
    for produces, module in STAGES:
        if STAGE_ORDER.index(m.stage) >= STAGE_ORDER.index(produces):
            print(f"[skip] {produces}")
            continue
        print(f"[run ] {module.__name__.split('.')[-1]} -> {produces}")
        m = module.run(m)
        m.save()
    print(f"\nDone. Final stage: {m.stage}")
    print(f"Open output/{chapter_id}/storyboard.html in your browser to see the result.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Manhwa recap pipeline orchestrator")
    ap.add_argument("--chapter", default="ch_001")
    ap.add_argument("--from", dest="from_stage", default=None, choices=STAGE_ORDER[1:])
    ap.add_argument("--force", action="store_true", help="re-run all stages")
    ap.add_argument("--type", dest="doc_type", default=None, choices=["manga", "webtoon"],
                    help="document type for a NEW chapter (else prompted once)")
    args = ap.parse_args()
    run(args.chapter, args.from_stage, args.force, args.doc_type)


if __name__ == "__main__":
    main()

"""Running the Claude script engine for a chapter as a job."""
from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import claude_engine
import config
import panel_split
import projects
import tones
import totals
from config import chapter_work_dir
from manifest import Manifest, Page, Panel
from services.chapter_files import _strip_path, downloaded_pdf, ensure_dir, make_preview, stitch_pdf
from services.editor_state import claude_state_path, manifest_panels

if TYPE_CHECKING:
    from services.jobs import Job


def _ensure_panels(chapter_id: str) -> list[Panel]:
    """The chapter's panels, split from its strip (stitching the downloaded PDF
    first if needed) and recorded in the manifest; kept only while they are newer than the strip."""
    panels = manifest_panels(chapter_id)
    work = chapter_work_dir(chapter_id)
    strip_path = _strip_path(chapter_id)
    if panels and strip_path.exists() and all(
            Path(p.crop).is_file() and Path(p.crop).stat().st_mtime >= strip_path.stat().st_mtime for p in panels):
        return panels
    pdf = downloaded_pdf(chapter_id)
    if not strip_path.exists():
        if pdf is None:
            raise RuntimeError("Download the chapter first.")
        make_preview(stitch_pdf(pdf, chapter_id), chapter_id)
    panels = panel_split.split_panels(strip_path, work / "panels")
    m = (Manifest.load(work) if (work / "manifest.json").exists() else
         Manifest(chapter_id=chapter_id, source_pdf=str(pdf or ""), work_dir=str(work), doc_type="webtoon"))
    m.pages = [Page(index=1, image=str(strip_path), panels=panels)]
    m.save()
    return panels


NARRATION_TYPES = {
    "first_person": 'first-person, as the hero living through these events ("I", "me")',
    "third_person": 'third-person recap narration ("he", "she", "they")',
}


def narration_voice(tone: str, narration_type: str) -> str:
    narr_desc = NARRATION_TYPES.get(narration_type, NARRATION_TYPES["first_person"])
    return f"Narrate {narr_desc}.\n\n{tones.style_instruction(tone)}"


def style_reference_directive(style_reference: str) -> str:
    return ("Match the LANGUAGE, TONE, and HUMOR of this example script as closely "
            "as you can - imitate its voice and style, not its plot or content:\n\n"
            + style_reference.strip())


def run_script_job(job: "Job") -> None:
    slug, ch_no, chapter_id = job.slug, job.ch_no, job.chapter_id
    read: set[str] = set()
    panel_ids: list[str] = []

    def set_progress(floor: float, span: float, done: int) -> None:
        job.fraction = floor + span * min(1.0, done / job.claude["panels_total"])

    def on_event(event: dict) -> None:
        stats, kind = job.claude, event["type"]
        if kind == "log":
            job.note(event["line"])
            return
        if kind == "plan":
            panel_ids[:] = event["sent"]
            stats["panels_total"] = len(panel_ids)
            job.note(f"Sending {len(panel_ids)} panels; {len(event['dropped'])} blank or uniform panels dropped locally")
        elif kind == "step":
            stats["pass"] = event["pass"]
            job.message = event["label"]
            job.note(event["label"] + "…")
            job.fraction = max(job.fraction, {1: 0.02, 2: 0.60, None: 0.95}[event["pass"]])
        elif kind == "read" and event["panel"] in panel_ids:
            read.add(event["panel"])
            stats["panels_read"] = len(read)
            set_progress(0.02, 0.58, len(read))
        elif kind == "cover" and event["panel"] in panel_ids:
            set_progress(0.60, 0.35, panel_ids.index(event["panel"]) + 1)
        elif kind == "usage":
            stats[f"pass{event['pass']}"] = event["usage"]
        stats["current"] = event.get("panel", stats["current"])
        job.emit(event)

    def on_limit(resume_at: datetime) -> None:
        job.resume_at = resume_at.isoformat(timespec="seconds")
        job.message = f"Usage limit reached - resumes at {resume_at:%H:%M}"
        job.note(job.message)
        while datetime.now().astimezone() < resume_at:
            if job.cancel_requested:
                raise RuntimeError("Stopped by user.")
            time.sleep(1)
        job.resume_at = None
        job.note("Usage limit reset - resuming.")

    def on_proc(proc: subprocess.Popen) -> None:
        job.proc = proc

    try:
        with config.chapter_scope(projects.chapter_work_dir(slug, ch_no), projects.chapter_output_dir(slug, ch_no)):
            settings = projects.load(slug)["settings"]
            job.message = "Splitting the strip into panels"
            job.fraction = 0.02
            split_started = time.monotonic()
            panels = _ensure_panels(chapter_id)
            job.note(f"{len(panels)} panels ready ({time.monotonic() - split_started:.0f}s)")
            job.claude = {"pass": None, "panels_read": 0, "panels_total": len(panels), "current": None,
                          "pass1": None, "pass2": None, "total": None}
            voice = settings["script"]
            tone = projects.chapter_tone(projects.load(slug), ch_no)
            job.note(f"Tone: {tones.TONES[tone]['label']}")
            style = [narration_voice(tone, voice["narration_type"])]
            if voice["style_reference"].strip():
                style.append(style_reference_directive(voice["style_reference"]))
            state_path = claude_state_path(chapter_id)
            ensure_dir(state_path.parent)
            rate = totals.speech_rate(slug)
            job.note(f"Speaking rate: {rate:.1f} chars/s, measured from this project's generated audio" if rate else
                     f"No generated audio in this project yet; assuming {claude_engine.DEFAULT_CHARS_PER_SEC} chars/s")
            narration = claude_engine.run_chapter(
                panels=panels,
                work_dir=state_path.parent,
                state_path=state_path,
                memory_path=projects.story_memory_path(slug),
                ch_no=int(ch_no),
                brief=settings["claude"]["series_brief"],
                style="\n\n".join(style),
                models={"comprehension": settings["claude"]["comprehension_model"],
                        "narration": settings["claude"]["narration_model"]},
                min_frame_s=settings["claude"]["min_frame_s"],
                chars_per_sec=rate or claude_engine.DEFAULT_CHARS_PER_SEC,
                on_event=on_event, on_limit=on_limit, on_proc=on_proc,
                before_call=job.before_claude_call)
            total = claude_engine.read_usage(state_path)["total"]
        job.claude["total"] = total
        job.emit({"type": "total", "usage": total})
        job.note(f"Chapter total: {total['input']:,} in, {total['output']:,} out, {total['cache_read']:,} cache read, "
                 f"{total['cache_write']:,} cache write; estimated API cost ${total['cost_usd']:.2f} "
                 "(not charged on subscription)")
        blocks = narration["blocks"]
        job.status, job.ok, job.fraction = "done", True, 1.0
        job.message = f"Narration ready - {len(blocks)} block(s), about {sum(b['duration_s'] for b in blocks) / 60:.1f} min."
    except Exception as e:  # noqa: BLE001 - report, don't crash the worker
        if job.cancel_requested:
            job.status, job.ok, job.message = "cancelled", False, "Stopped by user."
        else:
            job.status, job.ok = "failed", False
            job.message = f"{type(e).__name__}: {e}"
            job.note(f"ERROR: {job.message}")
    finally:
        job.resume_at = None

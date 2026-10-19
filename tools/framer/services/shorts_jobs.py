"""Running the find step of the Shorts pipeline for a chapter as a job (the render is stages/s8_shorts.py)."""
from __future__ import annotations

from typing import TYPE_CHECKING

import config
import projects
import shorts
from config import chapter_output_dir, chapter_work_dir
from services.script_jobs import attach_proc, wait_out_usage_limit
from stages import s7_assemble

if TYPE_CHECKING:
    from services.jobs import Job


def run_shorts_find_job(job: "Job") -> None:
    slug, ch_no, chapter_id = job.slug, job.ch_no, job.chapter_id

    def on_event(event: dict) -> None:
        if event["type"] == "log":
            job.note(event["line"])
        else:
            job.emit(event)

    try:
        with config.chapter_scope(projects.chapter_work_dir(slug, ch_no), projects.chapter_output_dir(slug, ch_no)):
            work, output = chapter_work_dir(chapter_id), chapter_output_dir(chapter_id)
            settings = projects.load(slug)["settings"]["claude"]
            beats = shorts.script_beats(work)
            job.message = "Choosing the strongest segments"
            job.fraction = 0.1
            job.note(f"Reading {len(beats)} script lines")
            found = shorts.find(
                chapter_no=int(ch_no), beats=beats, brief=settings["series_brief"], model=settings["comprehension_model"],
                work=work / "shorts", seconds_of=lambda run: s7_assemble.beats_duration(chapter_id, run),
                on_event=on_event, on_limit=lambda at: wait_out_usage_limit(job, at), on_proc=attach_proc(job),
                before_call=job.before_claude_call)
            for note in found["notes"]:
                job.note(note)
            shorts.save_plan(work, output, chapter_id, found["shorts"])
        usage = found["usage"]
        job.note(f"{usage['input']:,} in, {usage['output']:,} out; estimated API cost ${usage['cost_usd']:.2f} "
                 "(not charged on subscription)")
        job.status, job.ok, job.fraction = "done", True, 1.0
        job.message = (f"{len(found['shorts'])} short(s) planned." if found["shorts"]
                       else "No segment of this chapter is strong enough for a short.")
    except Exception as e:  # noqa: BLE001 - report, don't crash the worker
        if job.cancel_requested:
            job.status, job.ok, job.message = "cancelled", False, "Stopped by user."
        else:
            job.status, job.ok = "failed", False
            job.message = f"{type(e).__name__}: {e}"
            job.note(f"ERROR: {job.message}")
    finally:
        job.resume_at = None

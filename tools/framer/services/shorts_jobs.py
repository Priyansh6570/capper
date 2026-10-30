"""Running the find step of the Shorts pipeline over a range of chapters as one job (the render is stages/s8_shorts.py)."""
from __future__ import annotations

from typing import TYPE_CHECKING

import chapter_backup
import config
import projects
import short_music
import shorts
from services.script_jobs import attach_proc, wait_out_usage_limit
from stages import s7_assemble

if TYPE_CHECKING:
    from services.jobs import Job


def run_shorts_find_job(job: "Job") -> None:
    """Reads the script of every chapter in `job.chapters`, has Claude pick the strongest moments across all of
    them and saves each picked chapter's plan. `job.picked` ends up as {chapter: number of shorts planned}."""
    slug = job.slug

    def on_event(event: dict) -> None:
        if event["type"] == "log":
            job.note(event["line"])
        else:
            job.emit(event)

    def seconds_of(n: int, run: list) -> float:
        with config.chapter_scope(projects.chapter_work_path(slug, n), projects.chapter_output_path(slug, n)):
            return s7_assemble.beats_duration(f"{slug}_ch{n}", run)

    try:
        settings = projects.load(slug)["settings"]["claude"]
        scripts = {}
        for n in job.chapters:
            try:
                if chapter_backup.cleared_kinds(slug, n):   # its narration audio may be on Drive only
                    job.message = f"Restoring chapter {n} from the Drive backup"
                    chapter_backup.restore(slug, n, ["script", "audio"])
                scripts[n] = shorts.script_beats(projects.chapter_work_path(slug, n))
            except (OSError, ValueError, chapter_backup.BackupUnavailable) as e:
                job.note(f"Chapter {n} left out: {e}")
        if not scripts:
            raise ValueError("None of the chapters has a script with its narration audio.")
        job.message = f"Studying {len(scripts)} chapter(s) for the moments that would catch viewers"
        job.fraction = 0.05
        job.note(f"Reading {sum(len(b) for b in scripts.values())} script lines from chapters "
                 f"{', '.join(map(str, scripts))}")
        found = shorts.find_range(
            scripts=scripts, brief=settings["series_brief"], model=settings["comprehension_model"],
            work=projects.local_dir(slug) / "shorts_find", max_shorts=job.max_shorts, seconds_of=seconds_of,
            on_event=on_event, on_limit=lambda at: wait_out_usage_limit(job, at), on_proc=attach_proc(job),
            before_call=job.before_claude_call, on_progress=lambda fraction: setattr(job, "fraction", 0.05 + 0.9 * fraction))
        for note in found["notes"]:
            job.note(note)
        picked = sum(len(plan) for plan in found["shorts"].values())
        music_tracks = short_music.library(projects.load(slug))
        done = 0
        for n, plan in list(found["shorts"].items()):
            beats = scripts[n]
            for short in list(plan):
                job.message = f"Writing the narration of short {done + 1} of {picked}"
                job.fraction = 0.95 + 0.04 * done / max(1, picked)
                scene = [b for b in beats if short["first_line"] <= b.order <= short["last_line"]]
                after = [b for b in beats if b.order > short["last_line"]][:4]
                try:
                    short["script"] = shorts.write_script(
                        short, scene, after, brief=settings["series_brief"], model=settings["comprehension_model"],
                        work=projects.local_dir(slug) / "shorts_find", usage=found["usage"], on_event=on_event,
                        on_limit=lambda at: wait_out_usage_limit(job, at), on_proc=attach_proc(job),
                        before_call=job.before_claude_call, tracks=music_tracks or None)
                    track = next((t for t in music_tracks if t["id"] == short.get("track")), None)
                    short["duration_s"] = track["duration_s"] if track else shorts.script_seconds(short["script"])
                except ValueError as e:
                    plan.remove(short)
                    job.note(f"Chapter {n}: dropped \"{short['title']}\": {e}")
                done += 1
            if not plan:
                del found["shorts"][n]
            else:
                for i, short in enumerate(plan, start=1):
                    short["id"] = i
        for n, plan in found["shorts"].items():
            shorts.save_plan(projects.chapter_work_path(slug, n), projects.chapter_output_path(slug, n),
                             f"{slug}_ch{n}", plan)
            job.note(f"Chapter {n}: " + "; ".join(f"{s['title']} ({s['score']}/10)" for s in plan))
        job.picked = {n: len(plan) for n, plan in found["shorts"].items()}
        job.analyzed = sorted(scripts)
        usage = found["usage"]
        job.note(f"{usage['input']:,} in, {usage['output']:,} out; estimated API cost ${usage['cost_usd']:.2f} "
                 "(not charged on subscription)")
        job.status, job.ok, job.fraction = "done", True, 1.0
        total = sum(job.picked.values())
        job.message = (f"{total} short(s) found in {len(job.picked)} of {len(scripts)} chapter(s)." if total
                       else "No moment in these chapters is strong enough for a short.")
    except Exception as e:  # noqa: BLE001 - report, don't crash the worker
        if job.cancel_requested:
            job.status, job.ok, job.message = "cancelled", False, "Stopped by user."
        else:
            job.status, job.ok = "failed", False
            job.message = f"{type(e).__name__}: {e}"
            job.note(f"ERROR: {job.message}")
    finally:
        job.resume_at = None

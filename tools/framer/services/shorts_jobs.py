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
        music_tracks = short_music.library(projects.load(slug)) if config.SHORTS_MUSIC else []
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
                        before_call=job.before_claude_call, tracks=music_tracks or None,
                        other_title=projects.load(slug).get("other_title") or "")
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


DEFAULT_SHORTS = 10      # a run without a number makes this many shorts, one per chapter, from the first chapters of the range


def spread(chapters: list[int], total: int | None) -> dict[int, int]:
    """How many AMV shorts each chapter gets: one each, or `total` shorts dealt round-robin over the chapters."""
    if not total:
        return {n: 1 for n in chapters}
    counts = {n: 0 for n in chapters}
    for i in range(total):
        counts[chapters[i % len(chapters)]] += 1
    return {n: c for n, c in counts.items() if c}


def run_amv_find_job(job: "Job") -> None:
    """The find step of the AMV shorts: no Claude search, just make sure every chapter in `job.chapters` has its
    shorts frame bank (tagged frames; cut again from the chapter's saved mapping + PDF when the frames are gone) and
    decide how many shorts each gets. `job.picked` ends up as {chapter: number of shorts to render}."""
    from amv import bank
    from services import shorts_bank
    slug = job.slug
    try:
        ready = []
        total = job.max_shorts or DEFAULT_SHORTS          # prepare only as many chapters as shorts are wanted
        for i, n in enumerate(job.chapters):
            if len(ready) >= min(total, len(job.chapters)):
                break
            job.message = f"Preparing the frames of chapter {n} ({i + 1}/{len(job.chapters)})"
            job.fraction = i / max(1, len(job.chapters))
            work = projects.chapter_work_path(slug, n)
            try:
                shorts_bank.build_chapter(slug, n, log=job.note)
                if not bank.load(work) and chapter_backup.cleared_kinds(slug, n):
                    job.note(f"Chapter {n}: restoring its shorts frames from the Drive backup")
                    chapter_backup.restore(slug, n, ["shorts_frames"])
                if not bank.load(work):
                    job.note(f"Chapter {n}: the bank's frames are gone, cutting them again")
                    shorts_bank.build_chapter(slug, n, rebuild=True, log=job.note)
                if bank.load(work):
                    ready.append(n)
                else:
                    job.note(f"Chapter {n} left out: no usable frames.")
            except Exception as e:  # noqa: BLE001 - one bad chapter must not stop the rest
                job.note(f"Chapter {n} left out: {e}")
        if not ready:
            raise ValueError("None of the chapters has frames to make shorts from.")
        job.picked = spread(ready, min(job.max_shorts or DEFAULT_SHORTS, max(job.max_shorts or 0, len(ready))))
        job.analyzed = ready
        job.status, job.ok, job.fraction = "done", True, 1.0
        job.message = f"{sum(job.picked.values())} short(s) to make from {len(job.picked)} chapter(s)."
    except Exception as e:  # noqa: BLE001
        if job.cancel_requested:
            job.status, job.ok, job.message = "cancelled", False, "Stopped by user."
        else:
            job.status, job.ok, job.message = "failed", False, f"{type(e).__name__}: {e}"
            job.note(f"ERROR: {job.message}")
    finally:
        job.resume_at = None


def run_shorts_tag_job(job: "Job") -> None:
    """Tags one chapter's exported frames into its shorts frame bank (amv/bank.py, services/shorts_bank.py)."""
    from services import shorts_bank
    try:
        job.message = f"Tagging chapter {job.ch_no}'s frames for shorts"
        job.note(job.message)
        summary = shorts_bank.build_chapter(job.slug, int(job.ch_no), rebuild=getattr(job, "rebuild", False), log=job.note)
        job.message = f"Shorts bank: {summary['kept']} of {summary['frames']} frames kept"
        job.note(job.message)
        job.fraction, job.status, job.ok = 1.0, "done", True
    except Exception as e:      # noqa: BLE001 - the job reports it; a chapter with no frames is an ordinary outcome
        job.message = f"Shorts tagging failed: {e}"
        job.note(job.message)
        job.status, job.ok = "failed", False

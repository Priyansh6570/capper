"""Everything the batch pipeline needs from the app: chapter steps, thumbnails and stage runners."""
from __future__ import annotations

import dataclasses
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from PIL import Image
from flask import jsonify

import batch
import chapter_backup
import claude_cli
import claude_engine
import config
import gpu
import notify
import projects
import shorts
from publishing import enqueue, telegram
from config import NO_CONSOLE, PROJECTS_DIR, chapter_output_dir, chapter_work_dir
from manifest import Manifest
from services import cards
from services.chapter_files import downloaded_pdf, stitch_chapter
from services.common import Refused
from services.downloads import _download_chapter
from services.editor_state import resolve_editor_state
from services.frames import export_chapter
from services.jobs import JOBS, Job, new_render_job, new_script_job, new_shorts_job, run_now, stop_running


# --------------------------------------------------------------------------- #
# Batch automation (batch.py): the whole pipeline for several chapters of a project. Each
# stage runner calls the same functions the matching button's route does.
# --------------------------------------------------------------------------- #
BATCHES: dict[str, batch.Batch] = {}


SHORTS_BATCHES: dict[str, batch.Batch] = {}   # the Shorts pipeline of each project, a batch of its own


BATCH_LOCK = threading.Lock()


THUMB_WIDTH = 480


def _entries_from_state(state: dict) -> list[dict]:
    """Export entries for editor-state lines, as the editor's Export button builds them."""
    return [{"line_text": line["text"], "duration_s": line.get("duration_s") or 4, "mode": line.get("mode"),
             "pace": line.get("pace"),
             "frames": [{"bbox": f["bbox"], "flip": bool(f.get("flip")), "blur": f.get("blur") or []}
                        for f in line.get("frames", [])]} for line in state["lines"]]


def _chapter_pdf(chapter_id: str) -> Path:
    pdf = downloaded_pdf(chapter_id)
    if pdf is None:
        raise Refused("Download the chapter first.")
    return pdf


def _newest(paths) -> float | None:
    times = [p.stat().st_mtime for p in paths if p.is_file()]
    return max(times) if times else None


def _script_saved_at(state_path: Path) -> float | None:
    narration = claude_engine.read_narration(state_path)
    return datetime.fromisoformat(narration["saved_at"]).timestamp() if narration else None


def _stage_times(chapter_id: str) -> dict[str, float | None]:
    """When each pipeline step's output was last written, None for a step with no output. Creates
    nothing on disk, so it is safe to call for every chapter. Call inside its scope."""
    work = chapter_work_dir(chapter_id)
    try:
        beats = Manifest.load(work).beats
    except (OSError, ValueError):
        beats = []
    voiced = bool(beats) and all(beat.audio and Path(beat.audio).is_file() for beat in beats)
    return {
        "download": _newest((work / "download").rglob("*.pdf")),
        "stitch": _newest([work / "pages" / "strip_001.png"]),
        "script": _script_saved_at(work / "claude" / "state.json"),
        "export": _newest([work / "framer" / "mapping.json"]),
        "audio": _newest(Path(beat.audio) for beat in beats) if voiced else None,
        "video": _newest([chapter_output_dir(chapter_id) / "recap.mp4"]),
    }


# steps whose output was cleared from this PC after a Drive backup: they still count as done
_STAGE_OF_KIND = {"download": "download", "pages": "stitch", "audio": "audio", "video": "video"}


def chapter_steps(slug: str, n: int) -> dict:
    """The chapter's pipeline steps as the disk has them: `done` (a rendered video counts as having
    every step, older chapters never produced some intermediates), `stale` (done, but an earlier
    step was written after it), and `next`, the first step that is missing or stale."""
    with config.chapter_scope(projects.chapter_work_path(slug, n), projects.chapter_output_path(slug, n)):
        times = _stage_times(f"{slug}_ch{n}")
    cleared = {_STAGE_OF_KIND[k] for k in chapter_backup.cleared_kinds(slug, n) if k in _STAGE_OF_KIND}
    rendered = times["video"] is not None or "video" in cleared
    done = [s for s in batch.LANE_STAGES if rendered or times[s] is not None or s in cleared]
    stale = [s for i, s in enumerate(batch.LANE_STAGES)
             if times[s] is not None and any(t is not None and t > times[s] for t in map(times.get, batch.LANE_STAGES[:i]))]
    pending = [s for s in batch.LANE_STAGES if s not in done or s in stale]
    return {"done": done, "stale": stale, "next": pending[0] if pending else None}


def _stages_on_disk(chapter_id: str) -> list[str]:
    """The leading run of steps the chapter has output for: what a batch resumes from.
    Call inside its scope."""
    times = _stage_times(chapter_id)
    rendered = times["video"] is not None
    done = []
    for stage in batch.LANE_STAGES:
        if not rendered and times[stage] is None:
            break
        done.append(stage)
    return done


def thumb_source(slug: str, n: int) -> tuple[Path, bool] | None:
    """The file the chapter's thumbnail comes from (its first exported frame, else its rendered
    video) and whether that file is a frame."""
    frames = sorted((projects.chapter_work_path(slug, n) / "frames").glob("frame_*.png"))
    source = frames[0] if frames else projects.chapter_video_path(slug, n)
    return (source, bool(frames)) if source.is_file() else None


def chapter_thumb(slug: str, n: int) -> Path | None:
    """A small JPEG of the chapter's thumbnail source, rebuilt whenever that source is newer."""
    found = thumb_source(slug, n)
    if found is None:
        return None
    source, is_frame = found
    thumb = projects.chapter_work_dir(slug, n) / "thumb.jpg"
    if thumb.is_file() and thumb.stat().st_mtime >= source.stat().st_mtime:
        return thumb
    if is_frame:
        with Image.open(source) as image:
            image.convert("RGB").resize((THUMB_WIDTH, round(image.height * THUMB_WIDTH / image.width))).save(thumb, quality=82)
    else:
        done = subprocess.run([gpu.ffmpeg_path(), "-y", "-ss", "1", "-i", str(source), "-frames:v", "1",
                               "-vf", f"scale={THUMB_WIDTH}:-2", str(thumb)], capture_output=True, **NO_CONSOLE)
        if done.returncode:
            return None
    return thumb


def _cancel_job(job_id: str) -> None:
    job = JOBS.get(job_id)
    if job and job.status == "running":
        stop_running(job)


def _run_in_batch(b: batch.Batch, n: int, job: Job) -> None:
    b.attach(n, job.id)
    job.mirror = lambda line: b.log(n, line)
    run_now(job)
    if job.status != "done":
        raise RuntimeError(job.message or f"job {job.status}")


def _failed(slug: str, shorts: bool) -> Callable[[batch.Batch, int, str, str], None]:
    """What happens when a chapter fails at a stage: a desktop toast, and a Telegram card with the ways to go on."""
    def failed(b: batch.Batch, n: int, stage: str, error: str) -> None:
        if projects.load(slug)["settings"]["batch"]["notifications"]:
            notify.toast(f"Chapter {n} failed", f"{stage}: {error}")
        cards.announce_failure(slug, n, stage, error, shorts)
    return failed


def _pipeline_finished(slug: str, finished: batch.Batch) -> None:
    """The batch ran out of work: make shorts for the chapters it rendered if the project does that by itself, then
    offer to merge the project's finished chapters. Runs on its own thread: the batch is still locked."""
    rendered = [c["n"] for c in finished.chapters if c["state"] == "done" and "video" in c["plan"]]

    def after() -> None:
        if rendered and projects.load(slug)["settings"]["shorts"]["auto_make"]:
            try:
                start_shorts(slug, rendered)
            except Refused as e:
                telegram.notify(f"{projects.load(slug)['name']}: shorts were not started - {e}.")
        cards.propose_merge(slug)

    threading.Thread(target=after, daemon=True).start()


def batch_env(slug: str) -> batch.BatchEnv:
    def chapter_id(n: int) -> str:
        return f"{slug}_ch{n}"

    def batch_settings() -> dict:
        return projects.load(slug)["settings"]["batch"]

    def download(b: batch.Batch, n: int) -> None:
        _download_chapter(projects.load(slug)["url"], str(n), chapter_id(n), slug, str(n), lambda line: b.log(n, line))

    def stitch(b: batch.Batch, n: int) -> None:
        stitch_chapter(_chapter_pdf(chapter_id(n)), chapter_id(n))

    def script(b: batch.Batch, n: int) -> None:
        b.gate()
        job = new_script_job(chapter_id(n), slug, str(n))
        job.before_claude_call = b.gate
        _run_in_batch(b, n, job)

    def export(b: batch.Batch, n: int) -> None:
        state = resolve_editor_state(chapter_id(n))
        if not state or not state.get("lines"):
            raise Refused("There are no narration lines to export.")
        export_chapter(chapter_id(n), _entries_from_state(state), slug, str(n))

    def render(what: str) -> Callable[[batch.Batch, int], None]:
        return lambda b, n: _run_in_batch(b, n, new_render_job(chapter_id(n), what, slug, str(n)))

    def video(b: batch.Batch, n: int) -> None:
        render("video")(b, n)
        projects.set_status(slug, str(n), "complete")

    def announce(title: str, message: str) -> None:
        telegram.notify(f"{projects.load(slug)['name']}: {title}\n{message}")
        if batch_settings()["notifications"]:
            notify.toast(title, message)

    def space_low() -> bool:
        bt = batch_settings()
        return (bool(bt.get("auto_offload")) and chapter_backup.is_shared(slug)
                and shutil.disk_usage(PROJECTS_DIR).free / 1e9 < bt["offload_below_gb"])

    def offload(active: set[int]) -> int:
        """Upload every finished chapter that is not part of the running batch to Drive and clear it here."""
        return sum(chapter_backup.offload(slug, ch["n"]) for ch in projects.load(slug)["chapters"]
                   if ch["status"] == "complete" and ch["n"] not in active)

    return batch.BatchEnv(
        runners={"download": download, "stitch": stitch, "script": script,
                 "export": export, "audio": render("voice"), "video": video},
        scope=lambda n: config.chapter_scope(projects.chapter_work_dir(slug, n), projects.chapter_output_dir(slug, n)),
        done_stages=lambda n: _stages_on_disk(chapter_id(n)),
        review_enabled=lambda: batch_settings()["review_before_audio"],
        thresholds=lambda: {"five_hour": batch_settings()["five_hour_pct"],
                            "seven_day": batch_settings()["seven_day_pct"]},
        auto_wait=lambda: batch_settings()["auto_wait_five_hour"],
        cancel_job=_cancel_job, space_low=space_low, offload=offload,
        notify=announce, finished=lambda finished: _pipeline_finished(slug, finished),
        failed=_failed(slug, shorts=False),
        claude_total=lambda n: (claude_engine.read_usage(projects.chapter_work_path(slug, n) / "claude" / "state.json")
                                or {}).get("total"))


def shorts_env(slug: str) -> batch.BatchEnv:
    """The Shorts pipeline: Claude studies a range of finished chapters for the moments that would catch
    viewers, then those moments are rendered as vertical videos. The range is the batch's `meta["find"]`
    ({chapters, max_shorts}) and is studied once, as the first chapter's find step; the chapters it picked
    from join the batch there to be rendered. Everything but the steps is the project's batch environment."""
    def chapter_id(n: int) -> str:
        return f"{slug}_ch{n}"

    def find(b: batch.Batch, n: int) -> None:
        b.gate()
        wanted = b.meta.get("find") or {"chapters": [n]}   # a batch saved before ranges: its own chapter
        job = new_shorts_job(chapter_id(n), "shorts_find", slug, str(n))
        job.chapters, job.max_shorts = wanted["chapters"], wanted.get("max_shorts")
        job.before_claude_call = b.gate
        _run_in_batch(b, n, job)
        b.set_meta(found={"analyzed": job.analyzed, "picked": {str(c): count for c, count in job.picked.items()}})
        b.join({c: ["shorts_render"] for c in job.picked if c != n})

    def render(b: batch.Batch, n: int) -> None:
        found = b.meta.get("found")
        if found is not None and str(n) not in found["picked"]:
            b.log(n, "No moment of this chapter was picked, so nothing to render.")
            return
        if not shorts.read_plan(projects.chapter_work_path(slug, n)):
            b.log(n, "No segment was strong enough for a short, so nothing to render.")
            return
        _run_in_batch(b, n, new_shorts_job(chapter_id(n), "shorts_render", slug, str(n)))
        if queued := enqueue.shorts_of_chapter(slug, n):
            b.log(n, f"Queued {len(queued)} short(s) for upload.")

    return dataclasses.replace(
        batch_env(slug), runners={"shorts_find": find, "shorts_render": render}, stages=batch.SHORTS_STAGES,
        done_stages=lambda n: [], review_enabled=lambda: False, claude_total=lambda n: None,
        space_low=lambda: False, finished=lambda finished: None, failed=_failed(slug, shorts=True))


def shorts_blocker(slug: str, n: int) -> str | None:
    """Why chapter n can't have shorts made yet, or None. Shorts reuse the chapter's script, frames and
    narration, which a finished video has all of."""
    return None if "video" in chapter_steps(slug, n)["done"] else "it has no finished video yet"


def start_shorts(slug: str, numbers: list[int], max_shorts: int | None = None) -> tuple[list[int], list[dict]]:
    """Run the Shorts pipeline over those of `numbers` that have a finished video. Returns the chapters it studies
    and the ones skipped with the reason. Raises Refused when none can have shorts or shorts are being made already."""
    skipped = [{"n": n, "reason": reason} for n in numbers if (reason := shorts_blocker(slug, n))]
    eligible = [n for n in numbers if n not in {s["n"] for s in skipped}]
    if not eligible:
        raise Refused("None of these chapters can have shorts yet: " +
                      "; ".join(f"chapter {s['n']} {s['reason']}" for s in skipped) + ".", 409)
    with BATCH_LOCK:
        previous = SHORTS_BATCHES.get(slug)
        if previous and previous.active:
            raise Refused("Shorts are already being made for this project; wait for them or stop them first.", 409)
        if previous:
            previous.close()
        SHORTS_BATCHES[slug] = batch.Batch.create(
            shorts_batch_path(slug), shorts_env(slug), {eligible[0]: list(batch.SHORTS_STAGES)},
            meta={"find": {"chapters": eligible, "max_shorts": max_shorts, "skipped": skipped}})
    return eligible, skipped


def active_chapters(slug: str) -> set[int]:
    """The chapters the project's pipeline is working on or has queued."""
    current = BATCHES.get(slug)
    return {c["n"] for c in current.chapters if c["state"] in batch.ACTIVE_STATES} if current and current.active else set()


def pipeline_running() -> bool:
    return any(b.active for running in (BATCHES, SHORTS_BATCHES) for b in running.values())


def batch_path(slug: str) -> Path:
    return projects.local_dir(slug) / "batch.json"


def shorts_batch_path(slug: str) -> Path:
    return projects.local_dir(slug) / "shorts_batch.json"


def has_active_batch(slug: str) -> bool:
    return any(b and b.active for b in (BATCHES.get(slug), SHORTS_BATCHES.get(slug)))


def resume_batches() -> None:
    """Pick up each project's batches where the last run left them."""
    for folder in sorted(PROJECTS_DIR.iterdir()) if PROJECTS_DIR.is_dir() else []:
        if (folder / "batch.json").is_file() and (resumed := batch.Batch.load(batch_path(folder.name),
                                                                            batch_env(folder.name))):
            BATCHES[folder.name] = resumed
        if (folder / "shorts_batch.json").is_file() and (resumed := batch.Batch.load(shorts_batch_path(folder.name),
                                                                                    shorts_env(folder.name))):
            SHORTS_BATCHES[folder.name] = resumed


def job_summary(job_id: str) -> dict | None:
    job = JOBS.get(job_id)
    return {**{k: v for k, v in job.public().items() if k != "log"}, "last_activity": job.last_activity} if job else None


def start_batch(slug: str, plans: dict[int, list[str]]) -> None:
    """Run `plans` (chapter -> steps) as the project's batch, or add them to the one already running.
    Raises Refused when they cannot run."""
    if any("script" in steps for steps in plans.values()):
        status = claude_cli.cli_status()
        if not status["ready"]:
            raise Refused(status["message"], 409)
    with BATCH_LOCK:
        previous = BATCHES.get(slug)
        if previous and previous.active:
            try:
                previous.enqueue(plans)
            except ValueError as e:
                raise Refused(str(e), 409) from e
            return
        if previous:
            previous.close()
        BATCHES[slug] = batch.Batch.create(batch_path(slug), batch_env(slug), plans)


def batch_do(slug: str, action: Callable[[batch.Batch], None], store: dict[str, batch.Batch] = BATCHES):
    current = store.get(slug)
    if current is None:
        return jsonify(error="This project has no batch."), 404
    try:
        action(current)
    except ValueError as e:
        return jsonify(error=str(e)), 409
    return jsonify(ok=True)

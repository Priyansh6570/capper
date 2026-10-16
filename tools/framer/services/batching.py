"""Everything the batch pipeline needs from the app: chapter steps, thumbnails and stage runners."""
from __future__ import annotations

import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from PIL import Image
from flask import jsonify

import batch
import claude_engine
import config
import gpu
import notify
import projects
from config import NO_CONSOLE, PROJECTS_DIR, chapter_output_dir, chapter_work_dir
from manifest import Manifest
from services.chapter_files import downloaded_pdf, stitch_chapter
from services.common import Refused
from services.downloads import _download_chapter
from services.editor_state import resolve_editor_state
from services.frames import export_chapter
from services.jobs import JOBS, Job, new_render_job, new_script_job, run_now, stop_running


# --------------------------------------------------------------------------- #
# Batch automation (batch.py): the whole pipeline for several chapters of a project. Each
# stage runner calls the same functions the matching button's route does.
# --------------------------------------------------------------------------- #
BATCHES: dict[str, batch.Batch] = {}


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


def chapter_steps(slug: str, n: int) -> dict:
    """The chapter's pipeline steps as the disk has them: `done` (a rendered video counts as having
    every step, older chapters never produced some intermediates), `stale` (done, but an earlier
    step was written after it), and `next`, the first step that is missing or stale."""
    with config.chapter_scope(projects.chapter_work_path(slug, n), projects.chapter_output_path(slug, n)):
        times = _stage_times(f"{slug}_ch{n}")
    rendered = times["video"] is not None
    done = [s for s in batch.LANE_STAGES if rendered or times[s] is not None]
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


def batch_env(slug: str) -> batch.BatchEnv:
    def chapter_id(n: int) -> str:
        return f"{slug}_ch{n}"

    def batch_settings() -> dict:
        return projects.load(slug)["settings"]["batch"]

    def mirror(b: batch.Batch, n: int, line: str) -> None:
        b.log(n, line)

    def run_job(b: batch.Batch, n: int, job: Job) -> None:
        b.attach(n, job.id)
        job.mirror = lambda line: mirror(b, n, line)
        run_now(job)
        if job.status != "done":
            raise RuntimeError(job.message or f"job {job.status}")

    def download(b: batch.Batch, n: int) -> None:
        _download_chapter(projects.load(slug)["url"], str(n), chapter_id(n), slug, str(n), lambda line: b.log(n, line))

    def stitch(b: batch.Batch, n: int) -> None:
        stitch_chapter(_chapter_pdf(chapter_id(n)), chapter_id(n))

    def script(b: batch.Batch, n: int) -> None:
        b.gate()
        job = new_script_job(chapter_id(n), slug, str(n))
        job.before_claude_call = b.gate
        run_job(b, n, job)

    def export(b: batch.Batch, n: int) -> None:
        state = resolve_editor_state(chapter_id(n))
        if not state or not state.get("lines"):
            raise Refused("There are no narration lines to export.")
        export_chapter(chapter_id(n), _entries_from_state(state), slug, str(n))

    def render(what: str) -> Callable[[batch.Batch, int], None]:
        return lambda b, n: run_job(b, n, new_render_job(chapter_id(n), what, slug, str(n)))

    def video(b: batch.Batch, n: int) -> None:
        render("video")(b, n)
        projects.set_status(slug, str(n), "complete")

    return batch.BatchEnv(
        runners={"download": download, "stitch": stitch, "script": script,
                 "export": export, "audio": render("voice"), "video": video},
        scope=lambda n: config.chapter_scope(projects.chapter_work_dir(slug, n), projects.chapter_output_dir(slug, n)),
        done_stages=lambda n: _stages_on_disk(chapter_id(n)),
        review_enabled=lambda: batch_settings()["review_before_audio"],
        thresholds=lambda: {"five_hour": batch_settings()["five_hour_pct"],
                            "seven_day": batch_settings()["seven_day_pct"]},
        auto_wait=lambda: batch_settings()["auto_wait_five_hour"],
        cancel_job=_cancel_job,
        notify=lambda title, message: notify.toast(title, message) if batch_settings()["notifications"] else None,
        claude_total=lambda n: (claude_engine.read_usage(projects.chapter_work_path(slug, n) / "claude" / "state.json")
                                or {}).get("total"))


def batch_path(slug: str) -> Path:
    return projects.local_dir(slug) / "batch.json"


def resume_batches() -> None:
    """Pick up each project's batch where the last run left it."""
    for folder in sorted(PROJECTS_DIR.iterdir()) if PROJECTS_DIR.is_dir() else []:
        if (folder / "batch.json").is_file() and (resumed := batch.Batch.load(batch_path(folder.name),
                                                                            batch_env(folder.name))):
            BATCHES[folder.name] = resumed


def job_summary(job_id: str) -> dict | None:
    job = JOBS.get(job_id)
    return {**{k: v for k, v in job.public().items() if k != "log"}, "last_activity": job.last_activity} if job else None


def batch_do(slug: str, action: Callable[[batch.Batch], None]):
    current = BATCHES.get(slug)
    if current is None:
        return jsonify(error="This project has no batch."), 404
    try:
        action(current)
    except ValueError as e:
        return jsonify(error=str(e)), 409
    return jsonify(ok=True)

"""The render job queue: jobs, the worker, and running the pipeline as a subprocess."""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import branding
import claude_cli
import config
import library
import projects
from config import NO_CONSOLE, ROOT, chapter_output_dir, chapter_work_dir
from manifest import Manifest
from services.chapter_files import _strip_path, downloaded_pdf
from services.common import Refused, now_iso
from services.script_jobs import run_script_job


# --------------------------------------------------------------------------- #
# generate audio + video - a proper job QUEUE, not a request/SSE-stream-shaped
# job. Rationale (this replaced an earlier design where the SSE connection
# itself WAS the job - a request-scoped generator running the subprocess and
# yielding its stdout): a job's real state has to survive the browser
# navigating away and back, so it can't live only inside one HTTP connection.
# A Job is a plain object in the JOBS registry, run by ONE background worker
# thread pulled off QUEUE_ORDER in order (rendering is GPU-bound - see
# s6_tts.py - so jobs run one at a time, never in parallel, on purpose).
# Any route can ask JOBS for a job's CURRENT status at any time; the
# frontend polls GET /api/jobs (global, for the corner widget + queue) and
# GET /api/jobs/<id> (per-chapter detail) instead of trusting a live stream -
# so "navigate away and back" always reflects the real state, never a stale
# assumption.
# --------------------------------------------------------------------------- #
JOBS: dict[str, "Job"] = {}


QUEUE_ORDER: list[str] = []          # queued job ids, in run order


ACTIVE_CHAPTERS: set[str] = set()    # chapter_ids with a queued OR running job


JOBS_LOCK = threading.Lock()


_JOBS_COND = threading.Condition(JOBS_LOCK)


# Rolling per-`what` history of (seconds per beat) from completed jobs, used
# to estimate remaining time for running/queued jobs - see _rate_for(). Seed
# values are a rough guess (TTS dominates "both"/"voice"; "video" is mostly
# ffmpeg) used only until real history exists; capped so old outliers age out.
_RENDER_HISTORY: dict[str, list[float]] = {"both": [9.0], "voice": [7.0], "video": [2.5]}


_RENDER_HISTORY_CAP = 20


# Jobs whose length isn't per-beat: a branded join is a copy once the clips are
# cached; a script is two Claude calls over a whole chapter (a rough guess).
FIXED_ESTIMATE_S = {"brand": 15.0, "script": 600.0}


class Job:
    """One audio/video generate run. Deliberately NOT tied to any request or
    SSE connection - see module note above."""

    def __init__(self, chapter_id: str, what: str, slug: str | None, ch_no: str | None,
                 total_beats: int, target: str | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.chapter_id = chapter_id
        self.what = what                  # both|voice|video|brand|script
        self.slug = slug
        self.ch_no = ch_no
        self.target = target              # brand: the merged video being branded
        self.fraction = 0.0               # brand/script: progress reported by the job itself
        self.resume_at: str | None = None  # script: set while paused on Claude's usage limit
        self.status = "queued"            # queued|running|done|failed|cancelled
        self.total_beats = total_beats
        self.audio = 0
        self.video = 0
        self.message = ""
        # True while stages/s6_tts.py is fetching a not-yet-cached Chatterbox
        # model (see its _ensure_model_cached) - lets the UI show real, live
        # status instead of a progress bar stuck at 0% (audio/video counters
        # can't move yet since synthesis hasn't started). See "[voice-model]"
        # handling below.
        self.downloading_model = False
        self.video_url: str | None = None
        self.ok: bool | None = None
        self.log: list[str] = []
        self.created_at = now_iso()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.proc: subprocess.Popen | None = None
        self.cancel_requested = False
        self.claude: dict | None = None    # script: live pass / panel / token stats
        self.before_claude_call: Callable[[], None] | None = None   # script: a batch's usage-limit gate
        self.mirror: Callable[[str], None] | None = None   # a batch's per-chapter log
        self.last_activity = time.time()   # when the job last logged or emitted anything
        self.render = 0.0                  # video: share of the frames encoded, read from ffmpeg's output
        self.events: list[dict] = []       # script: pushed to the browser by /api/jobs/<id>/events
        self._events_cond = threading.Condition()

    def append_log(self, line: str) -> None:
        self.last_activity = time.time()
        self.log.append(line)
        if len(self.log) > 500:
            self.log.pop(0)
        if self.mirror:
            self.mirror(line)

    def emit(self, event: dict) -> None:
        self.last_activity = time.time()
        with self._events_cond:
            self.events.append(event)
            self._events_cond.notify_all()

    def note(self, line: str) -> None:
        self.append_log(line)
        self.emit({"type": "log", "line": line})

    def progress_fraction(self) -> float:
        """Mirrors the old client-side estimate, now computed server-side so
        ANY poller (corner widget, chapter detail, queue ETA) sees the same
        number: s6 prints one "[NNN] …" per voiced line, s7 one "line N: …"
        per rendered line. Audio is the slow part, weighted heavier for a
        combined run."""
        if self.what in FIXED_ESTIMATE_S:
            return self.fraction
        if self.total_beats <= 0 or (self.audio == 0 and self.video == 0 and self.render == 0):
            return 0.0
        a = min(1.0, self.audio / self.total_beats)
        v = min(1.0, self.video / self.total_beats)
        if self.what == "video":
            return min(1.0, 0.05 * v + 0.90 * self.render)
        if self.what == "voice":
            return min(1.0, 0.05 + 0.90 * a)
        return min(1.0, 0.05 + 0.70 * a + 0.03 * v + 0.22 * self.render)

    def estimated_total_s(self) -> float:
        if self.what in FIXED_ESTIMATE_S:
            return FIXED_ESTIMATE_S[self.what]
        return max(1.0, self.total_beats) * _rate_for(self.what)

    def eta_s(self, queued_ahead_s: float = 0.0) -> float | None:
        """Seconds until THIS job finishes: remaining run time, plus (for a
        queued job) however long everything ahead of it in line is estimated
        to still take - see jobs_with_eta()."""
        if self.status == "running" and self.what == "script":
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(self.started_at)).total_seconds()
            done = self.progress_fraction()
            return elapsed * (1.0 - done) / done if done > 0.05 else None
        if self.status == "running":
            return max(0.0, self.estimated_total_s() * (1.0 - self.progress_fraction()))
        if self.status == "queued":
            return queued_ahead_s + self.estimated_total_s()
        return None

    def public(self) -> dict:
        return {
            "id": self.id, "chapter_id": self.chapter_id, "what": self.what,
            "project": self.slug, "ch_no": self.ch_no, "target": self.target,
            "status": self.status,
            "total_beats": self.total_beats, "audio": self.audio, "video": self.video,
            "progress": round(self.progress_fraction(), 4) if self.status == "running" else
                        (1.0 if self.status == "done" else 0.0),
            "message": self.message, "video_url": self.video_url, "ok": self.ok,
            "resume_at": self.resume_at, "claude": self.claude,
            "downloading_model": self.downloading_model,
            "log": list(self.log), "created_at": self.created_at,
            "started_at": self.started_at, "finished_at": self.finished_at,
        }


def _rate_for(what: str) -> float:
    hist = _RENDER_HISTORY.get(what) or _RENDER_HISTORY["both"]
    return sum(hist) / len(hist)


def _record_history(job: "Job") -> None:
    if not (job.started_at and job.finished_at) or job.total_beats <= 0:
        return
    try:
        started = datetime.fromisoformat(job.started_at)
        finished = datetime.fromisoformat(job.finished_at)
        secs_per_beat = max(0.1, (finished - started).total_seconds() / job.total_beats)
    except Exception:  # noqa: BLE001 - a bad timestamp must not crash the worker
        return
    hist = _RENDER_HISTORY.setdefault(job.what, [])
    hist.append(secs_per_beat)
    del hist[:-_RENDER_HISTORY_CAP]


def jobs_with_eta() -> list[dict]:
    """All queued + running jobs, each with `eta_s` filled in, in the order
    they'll actually run (running job first, if any, then queue order) - the
    queue-position math (each job's wait = everything ahead of it) only makes
    sense computed together like this, not per-job in isolation."""
    with JOBS_LOCK:
        running = [j for j in JOBS.values() if j.status == "running"]
        queued = [JOBS[jid] for jid in QUEUE_ORDER if jid in JOBS]
    ordered = running + queued
    out = []
    ahead_s = 0.0
    for j in ordered:
        d = j.public()
        d["eta_s"] = j.eta_s(ahead_s)
        out.append(d)
        ahead_s = (d["eta_s"] if j.status == "running" else
                   (ahead_s + j.estimated_total_s()))
    return out


def _job_worker() -> None:
    while True:
        with _JOBS_COND:
            while not QUEUE_ORDER:
                _JOBS_COND.wait()
            job_id = QUEUE_ORDER.pop(0)
            job = JOBS.get(job_id)
        if job is None:
            continue
        if job.status == "cancelled":            # cancelled while still queued
            with JOBS_LOCK:
                ACTIVE_CHAPTERS.discard(job.chapter_id)
            continue
        _run_job(job)


threading.Thread(target=_job_worker, daemon=True).start()


# Hardware that must never serve two jobs at once, whether they come from the queue or from
# a batch's lanes: each job holds the locks for the resources it uses while it runs.
_HARDWARE = {name: threading.Lock() for name in ("claude", "audio", "video")}


_HARDWARE_USED = {"script": ("claude",), "voice": ("audio",), "video": ("video",),
                  "both": ("audio", "video"), "brand": ("video",)}


def _run_job(job: "Job") -> None:
    with contextlib.ExitStack() as held:
        for name in _HARDWARE_USED[job.what]:
            held.enter_context(_HARDWARE[name])
        job.status = "running"
        job.started_at = now_iso()
        try:
            {"brand": _run_brand_job, "script": run_script_job}.get(job.what, _run_pipeline_job)(job)
        finally:
            job.finished_at = now_iso()
            job.emit({"type": "end"})
            _record_history(job)
            with JOBS_LOCK:
                ACTIVE_CHAPTERS.discard(job.chapter_id)


def run_now(job: "Job") -> None:
    """Run `job` on the calling thread (a batch lane) instead of the queue's worker."""
    with JOBS_LOCK:
        JOBS[job.id] = job
        ACTIVE_CHAPTERS.add(job.chapter_id)
    _run_job(job)


_FFMPEG_PROGRESS_LINE = re.compile(r"^\w+=\s*\S*$")


def _ffmpeg_runner(job: "Job") -> branding.Runner:
    """Runs one ffmpeg command for `job`: streams `-progress` output into the
    job's progress fraction and its log, and lets cancel terminate the process."""
    def run(cmd, duration, on_fraction):
        job.append_log("$ " + " ".join(cmd))
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, bufsize=1, encoding="utf-8",
            errors="replace", **NO_CONSOLE)
        job.proc = proc
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                done_us = line.partition("=")[2]
                if duration > 0 and done_us.isdigit():
                    on_fraction(min(1.0, int(done_us) / 1e6 / duration))
            elif line and not _FFMPEG_PROGRESS_LINE.match(line):
                job.append_log(line)
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg exited with code {proc.returncode}")
    return run


def _run_brand_job(job: "Job") -> None:
    def on_fraction(f: float) -> None:
        job.fraction = f

    try:
        final = branding.render_final(job.slug, job.target, _ffmpeg_runner(job), on_fraction)
        job.status, job.ok, job.message = "done", True, "Done."
        job.fraction = 1.0
        job.video_url = f"/projects/{job.slug}/merged/{final.name}?v={int(final.stat().st_mtime)}"
    except Exception as e:  # noqa: BLE001 - report, don't crash the worker
        if job.cancel_requested:
            job.status, job.ok, job.message = "cancelled", False, "Stopped by user."
        else:
            job.status, job.ok = "failed", False
            job.message = f"{type(e).__name__}: {e}"
            job.append_log(f"ERROR: {job.message}")


_FFMPEG_TIME = re.compile(r"\btime=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _render_fraction(line: str, output_dir: Path) -> float | None:
    """How far a render has got, from one line of s7's output: ffmpeg's `time=` progress
    against the planned length. None for any other line."""
    if clock := _FFMPEG_TIME.search(line):
        try:
            total = json.loads((output_dir / "video_plan.json").read_text(encoding="utf-8"))["total_s"]
        except (OSError, ValueError, KeyError):
            return None
        return (int(clock[1]) * 3600 + int(clock[2]) * 60 + float(clock[3])) / max(1.0, total)
    return None


NATIVE_CRASHES = {0xC0000005: "an access violation", 0xC0000409: "a stack buffer overrun",
                  0xC00000FD: "a stack overflow", 0xC0000142: "a DLL failing to start"}


NATIVE_CRASH_RETRIES = 1


def _crash_message(code: int, log: list[str]) -> str:
    """Explain a pipeline child that died from a native crash (Python had no chance to raise), naming
    the Python line it was on when PYTHONFAULTHANDLER printed one."""
    current = max((i for i, line in enumerate(log) if line.startswith("Current thread")), default=None)
    frames = [] if current is None else [line.strip() for line in log[current:] if line.strip().startswith("File ")]
    where = f" at {frames[0]}" if frames else ""
    return (f"The pipeline crashed with {NATIVE_CRASHES[code]} (Windows exit code 0x{code:X}){where}. This is a "
            f"crash inside a native library, not a Python error; low free memory is a common trigger, so close "
            f"other heavy programs and run it again. The job log has the details.")


def _run_pipeline_job(job: "Job") -> None:
    slug, ch_no, chapter_id = job.slug, job.ch_no, job.chapter_id
    work_dir = projects.chapter_work_dir(slug, ch_no) if slug and ch_no else chapter_work_dir(chapter_id)
    output_dir = projects.chapter_output_dir(slug, ch_no) if slug and ch_no else chapter_output_dir(chapter_id)

    with config.chapter_scope(work_dir, output_dir):
        if slug and ch_no:
            projects.write_media_snapshot(slug, ch_no)   # a settings change must still land

        if job.what == "video":
            from_stage, to_stage = "video", None
        elif job.what == "voice":
            from_stage, to_stage = "audio", "audio"
        else:
            from_stage, to_stage = "audio", None

        # -u + PYTHONUNBUFFERED so each print streams immediately; utf-8 so
        # the child never dies on a unicode arrow. MANHWA_WORK_ROOT/OUTPUT_ROOT
        # hand the same scope to the orchestrator subprocess (config.py's
        # env-var fallback) - this worker thread has no Flask request context
        # for it to inherit.
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONFAULTHANDLER="1")
        if slug and ch_no:
            env["MANHWA_WORK_ROOT"] = str(work_dir)
            env["MANHWA_OUTPUT_ROOT"] = str(output_dir)
        cmd = [sys.executable, "-u", str(ROOT / "orchestrator.py"),
               "--chapter", chapter_id, "--from", from_stage]
        if to_stage:
            cmd += ["--to", to_stage]
        job.append_log("$ " + " ".join(cmd))

        proc = None
        try:
            for attempt in range(1 + NATIVE_CRASH_RETRIES):
                proc = subprocess.Popen(
                    cmd, cwd=str(ROOT), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, text=True, bufsize=1,
                    encoding="utf-8", errors="replace", **NO_CONSOLE)
                job.proc = proc

                for line in proc.stdout:                       # streams as it arrives
                    line = line.rstrip("\n")
                    job.append_log(line)
                    if re.match(r"^\s*\[\d{1,4}\]", line):
                        job.audio += 1
                        job.downloading_model = False
                    elif re.match(r"^\s*line \d+:", line):
                        job.video += 1
                        job.downloading_model = False
                    elif (done := _render_fraction(line, output_dir)) is not None:
                        job.render = max(job.render, done)
                    elif "[voice-model]" in line:
                        # see stages/s6_tts.py._ensure_model_cached - surfaces a
                        # not-yet-cached model download with a live message instead
                        # of a progress bar stuck at 0% (audio/video haven't moved
                        # yet since synthesis hasn't started).
                        job.message = line.strip()
                        job.downloading_model = "ready" not in line
                proc.wait()
                if job.cancel_requested or proc.returncode not in NATIVE_CRASHES or attempt == NATIVE_CRASH_RETRIES:
                    break
                job.append_log(_crash_message(proc.returncode, job.log) + " Trying once more.")
                job.audio = job.video = 0

            if job.cancel_requested:
                job.status, job.ok = "cancelled", False
                job.message = "Stopped by user."
            elif proc.returncode == 0:
                if job.what == "voice":
                    # audio-only: no video ran, so recap.mp4/status are
                    # untouched - a stale render stays valid until "Re-render
                    # video" is run over the new audio.
                    job.ok = True
                    job.message = "Audio regenerated. Re-render video to hear it in the recap."
                else:
                    mp4 = output_dir / "recap.mp4"
                    if mp4.exists() and slug and ch_no:
                        projects.set_status(slug, ch_no, "rendered")
                    job.ok = mp4.exists()
                    job.video_url = _video_url(chapter_id, mp4, slug, ch_no)
                    job.message = "Done." if mp4.exists() else "Finished, but recap.mp4 was not found."
                job.status = "done" if job.ok else "failed"
            else:
                job.status, job.ok = "failed", False
                job.message = (_crash_message(proc.returncode, job.log) if proc.returncode in NATIVE_CRASHES
                               else f"Pipeline exited with code {proc.returncode}.")
        except Exception as e:  # noqa: BLE001 - report, don't crash the worker
            job.status, job.ok = "failed", False
            job.message = f"{type(e).__name__}: {e}"
            job.append_log(f"ERROR: {job.message}")
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass


def _enqueue(job: "Job") -> None:
    with _JOBS_COND:
        JOBS[job.id] = job
        QUEUE_ORDER.append(job.id)
        ACTIVE_CHAPTERS.add(job.chapter_id)
        _JOBS_COND.notify()


def new_render_job(chapter_id: str, what: str, slug: str | None, ch_no: str | None) -> "Job":
    """A not-yet-started audio+video / voice / video job for a chapter whose frames are exported."""
    work_dir = projects.chapter_work_dir(slug, ch_no) if slug and ch_no else chapter_work_dir(chapter_id)
    with config.chapter_scope(work_dir, projects.chapter_output_dir(slug, ch_no)
                               if slug and ch_no else chapter_output_dir(chapter_id)):
        total = _beat_count(chapter_id)
    if not total:
        raise Refused(f"No narration lines for {chapter_id}. Export from the Framer first.")
    blockers = library.render_blockers(work_dir, what)
    if blockers:
        raise Refused(" ".join(blockers), 409)
    _refuse_if_busy(chapter_id, "A generate job")

    job = Job(chapter_id, what, slug, ch_no, total)
    label = {"both": "audio + video", "voice": "audio (voice only)", "video": "video"}[what]
    job.append_log(f"Queued {label} for {chapter_id} ({total or '?'} line(s))…")
    return job


def _refuse_if_busy(chapter_id: str, what: str) -> None:
    with JOBS_LOCK:
        if chapter_id in ACTIVE_CHAPTERS:
            raise Refused(f"{what} is already queued or running for {chapter_id}.", 409)


def _beat_count(chapter_id: str) -> int:
    """How many narration lines (beats) this chapter has, for the progress bar
    and time estimate. Must be called inside the right chapter_scope."""
    try:
        return len(Manifest.load(chapter_work_dir(chapter_id)).beats)
    except Exception:  # noqa: BLE001 - missing/old manifest -> unknown total
        return 0


def new_script_job(chapter_id: str, slug: str | None, ch_no: str | None) -> "Job":
    if not (slug and ch_no and projects.exists(slug)):
        raise Refused("Open a project chapter first.")
    status = claude_cli.cli_status()
    if not status["ready"]:
        raise Refused(status["message"], 409)
    if not (_strip_path(chapter_id).exists() or downloaded_pdf(chapter_id)):
        raise Refused("Download the chapter first.")
    _refuse_if_busy(chapter_id, "A job")
    job = Job(chapter_id, "script", slug, ch_no, 0)
    job.append_log(f"Queued Claude script for {chapter_id}…")
    return job


def stop_running(job: "Job") -> None:
    """Stop a running job's process and everything it started (a render's ffmpeg outlives
    its parent otherwise)."""
    job.cancel_requested = True
    if job.proc and job.proc.poll() is None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(job.proc.pid)], capture_output=True, **NO_CONSOLE)


def _video_url(chapter_id: str, mp4: Path, slug: str | None, ch_no: str | None) -> str | None:
    """URL of a rendered video, versioned by its mtime: stable while the file is
    unchanged (so the player isn't reloaded) and different after a re-render."""
    if not mp4.exists():
        return None
    scope = f"&project={slug}&ch_no={ch_no}" if slug and ch_no else ""
    return f"/output/{chapter_id}/{mp4.name}?v={int(mp4.stat().st_mtime)}{scope}"

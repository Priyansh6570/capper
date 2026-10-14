"""Batch automation: the whole pipeline for several chapters of one project, pipelined.

A batch holds an ordered list of chapters and one worker lane per stage. Chapters enter a
stage in order, one at a time, and a chapter moves on to its next stage the moment it
leaves one, so while chapter 1 is in audio chapter 2 can already be in Claude. Because
every lane takes one chapter at a time, the stages that share hardware (Claude, audio,
video) never run two chapters at once. A failed chapter stops only its own pipeline.

The engine knows nothing about Flask or the stages' internals: `BatchEnv` supplies the
stage runners and the few project lookups it needs. State lives in `batch.json`, so a
restarted app resumes each chapter from its last completed stage.

Claude work is gated on the subscription usage thresholds: `Batch.gate()` blocks while a
threshold is reached and the user has neither chosen to wait for the reset nor to continue.
With auto-wait on, a reached five-hour threshold waits for the reset by itself and resumes.

The batch also estimates when it will finish (from how long each step took before), warns when
a step goes quiet or a step goes quiet, and raises desktop notifications.
"""
from __future__ import annotations

import re
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import claude_usage
from atomic_io import atomic_write_json, read_json_with_backup_fallback

LANE_STAGES = ("download", "split", "stitch", "script", "export", "audio", "video")
REVIEW = "review"
ACTIVE_STATES = ("queued", "running", "waiting")
LOG_LINES = 3000
RESET_MARGIN_S = 60          # resume this long after a usage window's reset
STALL_AFTER_S = 300          # a running step with no output for this long is flagged
HISTORY_LEN = 8              # past durations kept per step for the finish-time estimate
DEFAULT_STEP_SECONDS = {"download": 60, "split": 20, "stitch": 20, "script": 600, "export": 30,
                        "audio": 600, "video": 600}
PROGRESS_LINE = re.compile(r"\d+%\||\bframe=\s*\d+|\d+/\d+ \[")   # tqdm / ffmpeg progress, shown as one updating line


class Stopped(RuntimeError):
    """The batch was cancelled while a stage was waiting."""


@dataclass
class BatchEnv:
    runners: dict[str, Callable[["Batch", int], None]]   # stage -> run it for chapter n (raises on failure)
    scope: Callable[[int], AbstractContextManager]        # redirects chapter paths to chapter n
    done_stages: Callable[[int], list[str]]               # stages whose output already exists on disk
    review_enabled: Callable[[], bool]
    thresholds: Callable[[], dict]                        # {"five_hour": pct, "seven_day": pct}
    auto_wait: Callable[[], bool]                         # wait out a five-hour limit without asking
    cancel_job: Callable[[str], None]
    notify: Callable[[str, str], None]                    # desktop notification (title, message)
    claude_total: Callable[[int], dict | None]            # tokens / cost / seconds Claude spent on chapter n
    usage: Callable[[], dict] = claude_usage.current


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Batch:
    def __init__(self, path: Path, env: BatchEnv, data: dict):
        self.path, self.env = path, env
        self.status = data["status"]                  # running | paused | finished | cancelled
        self.created_at = data["created_at"]
        self.finished_at = data.get("finished_at")
        self.ignore = data["ignore"]                  # thresholds the user chose to continue past
        self.chapters = data["chapters"]
        self.hold: dict | None = None                 # a reached threshold awaiting the user's choice
        self._live: dict[int, dict] = {}              # per chapter: message, job_id (not persisted)
        self._logs: dict[int, list[str]] = {}         # per chapter: everything its steps reported
        self._busy: set[int] = set()                  # chapters a lane is executing a step for right now
        self._warnings: dict[int, list[str]] = {}     # per chapter: things worth a look
        self._activity: dict[int, float] = {}         # per chapter: when its step last reported anything
        self._hold_ended = 0.0                        # when the last usage hold ended
        self._times = self._load_times()              # step -> recent durations, kept across batches
        self._generation = 0                          # bumped when a cancelled batch is revived
        self._closed = False
        self._cond = threading.Condition()
        for chapter in self.chapters:
            chapter.setdefault("plan", list(LANE_STAGES))
            if chapter["state"] == "running":
                chapter["state"] = "queued"
        self._settle()
        self._start_lanes()

    def _start_lanes(self) -> None:
        for stage in LANE_STAGES:
            threading.Thread(target=self._lane, args=(stage, self._generation), daemon=True).start()

    @classmethod
    def create(cls, path: Path, env: BatchEnv, plans: dict[int, list[str]]) -> "Batch":
        """`plans` maps each chapter to the steps to run, in pipeline order; every other step the
        chapter already has on disk counts as done."""
        chapters = [_new_chapter(env, n, plan) for n, plan in sorted(plans.items())]
        batch = cls(path, env, {"status": "running", "created_at": _now(), "finished_at": None,
                                "ignore": {w: False for w in claude_usage.WINDOWS}, "chapters": chapters})
        batch._save()
        return batch

    @classmethod
    def load(cls, path: Path, env: BatchEnv) -> "Batch | None":
        try:
            data = read_json_with_backup_fallback(path)
        except (OSError, ValueError):
            return None
        return cls(path, env, data)

    @property
    def active(self) -> bool:
        return self.status in ("running", "paused")

    def _save(self) -> None:
        atomic_write_json(self.path, {"status": self.status, "created_at": self.created_at,
                                      "finished_at": self.finished_at, "ignore": self.ignore,
                                      "chapters": self.chapters}, backup=False)

    def _load_times(self) -> dict[str, list[float]]:
        try:
            return read_json_with_backup_fallback(self.path.with_name("stage_times.json"))
        except (OSError, ValueError):
            return {}

    def _record_time(self, stage: str, seconds: float) -> None:
        history = self._times.setdefault(stage, [])
        history.append(round(seconds, 1))
        del history[:-HISTORY_LEN]
        atomic_write_json(self.path.with_name("stage_times.json"), self._times, backup=False)

    def _notify(self, title: str, message: str) -> None:
        try:
            self.env.notify(title, message)
        except Exception:  # noqa: BLE001 - a notification problem must never disturb the pipeline
            pass

    def _settle(self) -> None:
        if self.status in ("running", "paused") and not any(c["state"] in ACTIVE_STATES for c in self.chapters):
            self.status, self.finished_at = "finished", _now()
            done = sum(c["state"] == "done" for c in self.chapters)
            failed = sum(c["state"] == "failed" for c in self.chapters)
            self._notify("Pipeline finished", f"{done} of {len(self.chapters)} chapters complete"
                         + (f", {failed} failed" if failed else "") + ".")

    def _chapter(self, n: int) -> dict:
        return next(c for c in self.chapters if c["n"] == n)

    def _stopped(self) -> bool:
        return self._closed or self.status == "cancelled"

    def _raise_if_stopped(self) -> None:
        if self._stopped():
            raise Stopped("Batch cancelled.")

    # --- lanes --------------------------------------------------------------
    def _next_queued(self, stage: str) -> dict | None:
        if self.status != "running":
            return None
        return next((c for c in self.chapters if c["stage"] == stage and c["state"] == "queued"), None)

    def _claim(self, stage: str, generation: int) -> int | None:
        with self._cond:
            while not self._stopped() and generation == self._generation:
                chapter = self._next_queued(stage)
                if chapter:
                    chapter.update(state="running", error=None, stage_started_at=_now(),
                                   started_at=chapter["started_at"] or _now())
                    self._live[chapter["n"]] = {"started": time.time()}
                    self._warnings.pop(chapter["n"], None)
                    self._busy.add(chapter["n"])
                    self.log(chapter["n"], f"== {stage} started ==")
                    self._save()
                    return chapter["n"]
                self._cond.wait()
        return None

    def _lane(self, stage: str, generation: int) -> None:
        while (n := self._claim(stage, generation)) is not None:
            started, error = time.monotonic(), None
            try:
                with self.env.scope(n):
                    self.env.runners[stage](self, n)
            except Exception as e:  # noqa: BLE001 - a stage failure belongs to its chapter, not the lane
                error = f"{type(e).__name__}: {e}"
            self._leave(n, stage, error, time.monotonic() - started, generation)

    def _leave(self, n: int, stage: str, error: str | None, seconds: float, generation: int) -> None:
        with self._cond:
            self._busy.discard(n)
            if generation != self._generation:
                return
            chapter = self._chapter(n)
            self.log(n, f"== {stage} failed: {error} ==" if error else f"== {stage} finished in {seconds:.0f}s ==")
            chapter["stage_seconds"][stage] = round(chapter["stage_seconds"].get(stage, 0) + seconds, 1)
            chapter["stage_started_at"] = None
            started = self._live.get(n, {}).get("started", 0)
            if self.status == "cancelled":
                chapter["state"] = "cancelled"
            elif error:
                chapter.update(state="failed", error=f"{stage}: {error}", finished_at=_now())
                self._notify(f"Chapter {n} failed", f"{stage}: {error}")
            else:
                if self._hold_ended < started:
                    self._record_time(stage, seconds)
                _advance(chapter, stage, review=self.env.review_enabled())
                if chapter["state"] == "done":
                    self._notify(f"Chapter {n} is complete", "Its video is rendered.")
                elif chapter["state"] == "waiting":
                    self._notify(f"Chapter {n} is ready for review", "Check its frames, then continue to audio.")
            self._settle()
            self._save()
            self._cond.notify_all()

    # --- reported by stage runners -----------------------------------------
    def report(self, n: int, message: str) -> None:
        self._live.setdefault(n, {})["message"] = message
        self.log(n, message)

    def warn(self, n: int, warning: str) -> None:
        """Flag something worth a look on the chapter until its next step starts."""
        warnings = self._warnings.setdefault(n, [])
        if warning not in warnings:
            warnings.append(warning)
            self.log(n, f"!! {warning}")

    def log(self, n: int, line: str) -> None:
        """Add a line to the chapter's log; a progress line replaces the previous progress line."""
        self._activity[n] = time.time()
        lines = self._logs.setdefault(n, [])
        stamped = f"{datetime.now():%H:%M:%S} {line.rstrip()}"
        if lines and PROGRESS_LINE.search(line) and PROGRESS_LINE.search(lines[-1]):
            lines[-1] = stamped
        else:
            lines.append(stamped)
            del lines[:-LOG_LINES]

    def attach(self, n: int, job_id: str) -> None:
        self._live.setdefault(n, {})["job_id"] = job_id

    # --- controls -----------------------------------------------------------
    def pause(self) -> None:
        with self._cond:
            if self.status == "running":
                self.status = "paused"
                self._save()

    def resume(self) -> None:
        with self._cond:
            if self.status == "paused":
                self.status = "running"
                self._save()
                self._cond.notify_all()

    def cancel(self) -> None:
        with self._cond:
            if not self.active:
                return
            self.status, self.hold, self.finished_at = "cancelled", None, _now()
            job_ids = [self._live.get(c["n"], {}).get("job_id") for c in self.chapters if c["state"] == "running"]
            for chapter in self.chapters:
                if chapter["state"] in ACTIVE_STATES:
                    chapter["state"] = "cancelled"
            self._save()
            self._cond.notify_all()
        for job_id in filter(None, job_ids):
            self.env.cancel_job(job_id)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def enqueue(self, plans: dict[int, list[str]]) -> None:
        """Add `plans` (chapter -> steps to run) to this running batch: chapters it already holds start
        over with their new plan, others join. A chapter still working on a step can't be changed."""
        with self._cond:
            if self._closed:
                raise ValueError("This batch has been replaced by a newer one.")
            working = [n for n in plans if n in self._busy or any(c["n"] == n and c["state"] == "running" for c in self.chapters)]
            if working:
                raise ValueError(f"Chapter {', '.join(map(str, working))} is still working on a step; wait for it or cancel first.")
            for n, plan in plans.items():
                fresh = _new_chapter(self.env, n, plan)
                self.chapters = sorted([c for c in self.chapters if c["n"] != n] + [fresh], key=lambda c: c["n"])
                self.log(n, f"== queued to run {' + '.join(plan)} ==")
            self._save()
            self._cond.notify_all()

    def continue_review(self, n: int) -> None:
        with self._cond:
            chapter = self._chapter(n)
            if chapter["state"] != "waiting":
                raise ValueError(f"Chapter {n} is not waiting for review.")
            _advance(chapter, REVIEW, review=False)
            self._save()
            self._cond.notify_all()

    # --- usage limiter -------------------------------------------------------
    def _reached(self) -> list[dict]:
        usage, limits = self.env.usage(), self.env.thresholds()
        return [{"window": w, "percent": usage[w]["percent"], "threshold": limits[w],
                 "resets_at": usage[w]["resets_at"]}
                for w in claude_usage.WINDOWS
                if w in usage and not self.ignore[w] and usage[w]["percent"] >= limits[w]]

    def gate(self) -> None:
        """Return when Claude work may start. While a threshold is reached this blocks until
        the window resets and usage has been checked again (waiting is automatic for the
        five-hour limit when auto-wait is on), or until the user chooses to continue anyway."""
        while True:
            self._raise_if_stopped()
            windows = self._reached()
            if not windows:
                return
            resume_at = max(w["resets_at"] for w in windows) + RESET_MARGIN_S
            automatic = self.env.auto_wait() and all(w["window"] == "five_hour" for w in windows)
            with self._cond:
                self.hold = {"choice": "wait" if automatic else None, "automatic": automatic,
                             "windows": windows, "resume_at": resume_at}
            reached = ", ".join(f"{w['window'].replace('_', '-')} usage {w['percent']:.0f}%" for w in windows)
            resumes = f"{datetime.fromtimestamp(resume_at):%H:%M}"
            self._notify("Claude usage limit reached",
                         f"{reached}. Waiting until {resumes}; the pipeline resumes by itself." if automatic
                         else f"{reached}. Open the pipeline to choose: wait for the reset ({resumes}) or continue.")
            with self._cond:
                while self.hold is not None and not self._stopped():
                    if self.hold["choice"] == "wait" and time.time() >= resume_at:
                        break
                    self._cond.wait(timeout=5)
                self._raise_if_stopped()
                chose_continue = self.hold is None
                self.hold = None
                self._hold_ended = time.time()
            if chose_continue:
                return
            self._notify("Pipeline resuming", "The usage window has reset.")

    def choose(self, choice: str) -> None:
        with self._cond:
            if self.hold is None:
                raise ValueError("No usage limit is waiting for a decision.")
            if choice == "continue":
                for window in self.hold["windows"]:
                    self.ignore[window["window"]] = True
                self.hold = None
                self._save()
            elif choice == "wait":
                self.hold["choice"] = "wait"
            else:
                raise ValueError("choice must be 'wait' or 'continue'.")
            self._cond.notify_all()

    # --- view -----------------------------------------------------------------
    def _step_estimates(self) -> dict[str, float]:
        return {stage: sum(history) / len(history) if (history := self._times.get(stage))
                else float(DEFAULT_STEP_SECONDS[stage]) for stage in LANE_STAGES}

    def _estimate(self) -> dict[int, float]:
        """When (epoch seconds) each unfinished chapter should be done, simulating the lanes:
        a chapter enters a step once it has left the previous one and the step's lane is free."""
        now = time.time()
        step = self._step_estimates()
        lane_free = {stage: now for stage in LANE_STAGES}
        if self.hold:
            lane_free["script"] = self.hold["resume_at"]
        finish = {}
        for chapter in self.chapters:
            if chapter["state"] not in ACTIVE_STATES:
                continue
            ready = now
            upcoming = [s for s in chapter["plan"] if s not in chapter["completed"]]
            for stage in upcoming:
                if chapter["state"] == "running" and stage == chapter["stage"] and chapter["stage_started_at"]:
                    elapsed = now - datetime.fromisoformat(chapter["stage_started_at"]).timestamp()
                    end = now + max(step[stage] - elapsed, step[stage] * 0.1)
                else:
                    end = max(ready, lane_free[stage]) + step[stage]
                lane_free[stage] = end
                ready = end
            finish[chapter["n"]] = ready
        return finish

    def view(self, job_of: Callable[[str], dict | None]) -> dict:
        with self._cond:
            now = time.time()
            finish = self._estimate()
            chapters = []
            for chapter in self.chapters:
                n = chapter["n"]
                live = self._live.get(n, {})
                job = job_of(live["job_id"]) if live.get("job_id") else None
                warnings = list(self._warnings.get(n, []))
                quiet = now - max(self._activity.get(n, 0), (job or {}).get("last_activity", 0))
                if chapter["state"] == "running" and not self.hold and quiet > STALL_AFTER_S:
                    warnings.append(f"No output for {quiet / 60:.0f} min - this step may be stuck.")
                chapters.append({**chapter, "message": live.get("message"), "log": list(self._logs.get(n, [])),
                                 "job": job, "warnings": warnings, "claude": self.env.claude_total(n),
                                 "finish_at": finish.get(n)})
            return {"status": self.status, "created_at": self.created_at, "finished_at": self.finished_at,
                    "hold": self.hold, "ignore": dict(self.ignore), "chapters": chapters,
                    "finish_at": max(finish.values(), default=None)}


def _new_chapter(env: BatchEnv, n: int, plan: list[str]) -> dict:
    with env.scope(n):
        completed = [s for s in env.done_stages(n) if s not in plan]
    chapter = {"n": n, "plan": plan, "completed": completed, "stage": None, "state": "queued", "error": None,
               "started_at": None, "finished_at": None, "stage_started_at": None, "stage_seconds": {}}
    _advance(chapter, None, review=False)
    return chapter


def _advance(chapter: dict, left: str | None, *, review: bool) -> None:
    """Record that `chapter` left `left` and queue its next stage (or finish it)."""
    if left:
        chapter["completed"].append(left)
    upcoming = next((s for s in chapter["plan"] if s not in chapter["completed"]), None)
    if upcoming is None:
        chapter.update(state="done", stage=None, finished_at=_now())
    elif left == "export" and review:
        chapter.update(state="waiting", stage=REVIEW)
    else:
        chapter.update(state="queued", stage=upcoming)

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
"""
from __future__ import annotations

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


class Stopped(RuntimeError):
    """The batch was cancelled while a stage was waiting."""


@dataclass
class BatchEnv:
    runners: dict[str, Callable[["Batch", int], None]]   # stage -> run it for chapter n (raises on failure)
    scope: Callable[[int], AbstractContextManager]        # redirects chapter paths to chapter n
    done_stages: Callable[[int], list[str]]               # stages whose output already exists on disk
    review_enabled: Callable[[], bool]
    thresholds: Callable[[], dict]                        # {"five_hour": pct, "seven_day": pct}
    cancel_job: Callable[[str], None]
    usage: Callable[[], dict] = claude_usage.current


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Batch:
    def __init__(self, path: Path, env: BatchEnv, data: dict):
        self.path, self.env = path, env
        self.status = data["status"]                  # running | paused | finished | cancelled
        self.created_at = data["created_at"]
        self.ignore = data["ignore"]                  # thresholds the user chose to continue past
        self.chapters = data["chapters"]
        self.hold: dict | None = None                 # a reached threshold awaiting the user's choice
        self._live: dict[int, dict] = {}              # per chapter: message, job_id (not persisted)
        self._closed = False
        self._cond = threading.Condition()
        for chapter in self.chapters:
            if chapter["state"] == "running":
                chapter["state"] = "queued"
        self._settle()
        for stage in LANE_STAGES:
            threading.Thread(target=self._lane, args=(stage,), daemon=True).start()

    @classmethod
    def create(cls, path: Path, env: BatchEnv, numbers: list[int]) -> "Batch":
        chapters = []
        for n in sorted(numbers):
            with env.scope(n):
                completed = env.done_stages(n)
            chapters.append({"n": n, "completed": completed, "stage": None, "state": "queued", "error": None,
                             "started_at": None, "finished_at": None, "stage_started_at": None,
                             "stage_seconds": {}})
            _advance(chapters[-1], None, review=False)
        batch = cls(path, env, {"status": "running", "created_at": _now(),
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
                                      "ignore": self.ignore, "chapters": self.chapters}, backup=False)

    def _settle(self) -> None:
        if self.status in ("running", "paused") and not any(c["state"] in ACTIVE_STATES for c in self.chapters):
            self.status = "finished"

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

    def _claim(self, stage: str) -> int | None:
        with self._cond:
            while not self._stopped():
                chapter = self._next_queued(stage)
                if chapter:
                    chapter.update(state="running", error=None, stage_started_at=_now(),
                                   started_at=chapter["started_at"] or _now())
                    self._live[chapter["n"]] = {}
                    self._save()
                    return chapter["n"]
                self._cond.wait()
        return None

    def _lane(self, stage: str) -> None:
        while (n := self._claim(stage)) is not None:
            started, error = time.monotonic(), None
            try:
                with self.env.scope(n):
                    self.env.runners[stage](self, n)
            except Exception as e:  # noqa: BLE001 - a stage failure belongs to its chapter, not the lane
                error = f"{type(e).__name__}: {e}"
            self._leave(n, stage, error, time.monotonic() - started)

    def _leave(self, n: int, stage: str, error: str | None, seconds: float) -> None:
        with self._cond:
            chapter = self._chapter(n)
            chapter["stage_seconds"][stage] = round(chapter["stage_seconds"].get(stage, 0) + seconds, 1)
            chapter["stage_started_at"] = None
            if self.status == "cancelled":
                chapter["state"] = "cancelled"
            elif error:
                chapter.update(state="failed", error=f"{stage}: {error}", finished_at=_now())
            else:
                _advance(chapter, stage, review=self.env.review_enabled())
            self._settle()
            self._save()
            self._cond.notify_all()

    # --- reported by stage runners -----------------------------------------
    def report(self, n: int, message: str) -> None:
        self._live.setdefault(n, {})["message"] = message

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
            self.status, self.hold = "cancelled", None
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

    def retry(self, n: int) -> None:
        """Run a failed chapter again from the stage it failed in."""
        with self._cond:
            chapter = self._chapter(n)
            if chapter["state"] != "failed" or self.status not in ("running", "paused", "finished"):
                raise ValueError(f"Chapter {n} has not failed.")
            chapter.update(state="queued", error=None, finished_at=None)
            if self.status == "finished":
                self.status = "running"
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
        the user chooses (see `choose`): continue returns at once, wait returns after the
        window resets and usage has been checked again."""
        while True:
            self._raise_if_stopped()
            windows = self._reached()
            if not windows:
                return
            resume_at = max(w["resets_at"] for w in windows)
            with self._cond:
                self.hold = {"choice": None, "windows": windows, "resume_at": resume_at}
                while self.hold is not None and not self._stopped():
                    if self.hold["choice"] == "wait" and time.time() >= resume_at:
                        break
                    self._cond.wait(timeout=5)
                self._raise_if_stopped()
                chose_continue = self.hold is None
                self.hold = None
            if chose_continue:
                return

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
    def view(self, job_of: Callable[[str], dict | None]) -> dict:
        with self._cond:
            chapters = []
            for chapter in self.chapters:
                live = self._live.get(chapter["n"], {})
                chapters.append({**chapter, "message": live.get("message"),
                                 "job": job_of(live["job_id"]) if live.get("job_id") else None})
            return {"status": self.status, "created_at": self.created_at, "hold": self.hold,
                    "ignore": dict(self.ignore), "chapters": chapters}


def _advance(chapter: dict, left: str | None, *, review: bool) -> None:
    """Record that `chapter` left `left` and queue its next stage (or finish it)."""
    if left:
        chapter["completed"].append(left)
    upcoming = next((s for s in LANE_STAGES if s not in chapter["completed"]), None)
    if upcoming is None:
        chapter.update(state="done", stage=None, finished_at=_now())
    elif left == "export" and review:
        chapter.update(state="waiting", stage=REVIEW)
    else:
        chapter.update(state="queued", stage=upcoming)

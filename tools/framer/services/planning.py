"""A project's upload plan: how much finished video is ready, how long it lasts at the project's upload
rhythm, and which chapters to make or merge next. Only reads; the Telegram planner (cards.py, remote.py) acts on it."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import branding
import projects
from publishing import uploads

DEFAULT_CHAPTER_MINUTES = 8.0       # until the project has a rendered chapter to average
FINISHED = ("rendered", "complete")
IN_QUEUE = ("waiting_approval", "queued", "uploading")   # a full video on its way to YouTube


@dataclass
class Plan:
    slug: str
    name: str
    has_channel: bool
    days: list[int]                  # upload weekdays, 0 = Monday
    min_minutes: int
    avg_minutes: float               # per chapter, from the project's rendered videos
    per_upload: int                  # chapters one upload needs
    next_upload: date
    chapters: list[int]              # every chapter number, ascending
    minutes: dict[int, float]        # length of each finished chapter
    queued: set[int]                 # in a full video waiting for approval or upload
    uploaded: set[int]
    merged: set[int]                 # in a merged video not queued yet
    unmerged: set[int]               # finished, in no merged video
    buffer_minutes: float
    buffer_days: float
    suggestion: list[int]            # the next chapters to make for one upload
    suggestion_minutes: float

    def length_of(self, numbers: list[int]) -> float:
        return sum(self.minutes.get(n, self.avg_minutes) for n in numbers)


def chapter_minutes(slug: str, n: int) -> float | None:
    video = projects.chapter_video_path(slug, n)
    if not video.is_file():
        return None
    return branding.duration_of(video) / 60 or None


def _next_upload_day(days: list[int]) -> date:
    today = date.today()
    return next(today + timedelta(d) for d in range(7) if (today + timedelta(d)).weekday() in days)


def build(slug: str, busy: set[int]) -> Plan:
    """The plan of project `slug`; `busy` are the chapters a pipeline is working on, which are not suggested again."""
    project = projects.load(slug)
    settings = project["settings"]
    numbers = [c["n"] for c in project["chapters"]]
    finished = [c["n"] for c in project["chapters"] if c["status"] in FINISHED]
    minutes = {n: m for n in finished if (m := chapter_minutes(slug, n))}
    avg = sum(minutes.values()) / len(minutes) if minutes else DEFAULT_CHAPTER_MINUTES
    full = [i for i in uploads.all_items() if i["kind"] == uploads.FULL and i["project"] == slug]
    queued = {n for i in full if i["status"] in IN_QUEUE for n in i["chapters"]}
    uploaded = {n for i in full if i["status"] in uploads.UPLOADED for n in i["chapters"]}
    merged = branding.merged_chapters(slug) - queued - uploaded
    unmerged = set(finished) - queued - uploaded - merged
    wanted, days = settings["plan"]["min_minutes"], settings["plan"]["days"]
    per_upload = max(1, math.ceil(wanted / avg))
    ready = (queued | merged | unmerged) & set(numbers)
    buffer_minutes = sum(minutes.get(n, avg) for n in ready)
    todo = [n for n in numbers if n not in finished and n not in busy][:per_upload]
    return Plan(slug=slug, name=project["name"], has_channel=bool(settings["youtube"]["channel"]), days=days,
                min_minutes=wanted, avg_minutes=avg, per_upload=per_upload, next_upload=_next_upload_day(days),
                chapters=numbers, minutes=minutes, queued=queued & set(numbers), uploaded=uploaded & set(numbers),
                merged=merged & set(numbers), unmerged=unmerged & set(numbers), buffer_minutes=buffer_minutes,
                buffer_days=round(int(buffer_minutes // wanted) * 7 / len(days), 1),
                suggestion=todo, suggestion_minutes=len(todo) * avg)


def merge_proposal(plan: Plan) -> list[int]:
    """The earliest finished chapters that are not merged yet, in a row, up to the minutes one upload needs.
    Shorter than that when the chapters run out or a chapter that is not finished interrupts them."""
    picked: list[int] = []
    for n in plan.chapters:
        if n in plan.queued | plan.uploaded | plan.merged:
            continue
        if n not in plan.unmerged:
            break
        picked.append(n)
        if plan.length_of(picked) >= plan.min_minutes:
            break
    return picked


def check_range(plan: Plan, numbers: list[int]) -> None:
    """Raise ValueError naming why `numbers` cannot be merged into one upload."""
    for n in numbers:
        if n not in plan.chapters:
            raise ValueError(f"{plan.name} has no chapter {n}.")
        if n not in plan.minutes:
            raise ValueError(f"Chapter {n} has no finished video.")
        if n in plan.queued | plan.uploaded:
            raise ValueError(f"Chapter {n} is already in an upload.")


def neediest(plans: list[Plan]) -> Plan | None:
    """The plan with the smallest buffer among those that have a channel and chapters left to make."""
    candidates = [p for p in plans if p.has_channel and p.suggestion]
    return min(candidates, key=lambda p: (p.buffer_days, p.name), default=None)

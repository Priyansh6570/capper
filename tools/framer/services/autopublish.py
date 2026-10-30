"""Publishing chapters the user approved to merge (Telegram's merge card): merge them, add the intro and outro, and
queue the video on the project's YouTube channel for approval."""
from __future__ import annotations

import threading
import time

import branding
import projects
from publishing import enqueue, telegram
from publishing.labels import chapter_label
from services.common import Refused
from services.jobs import Job, _enqueue
from services.merging import brand_key, merge_videos

JOB_POLL_S = 2


def publish_in_background(slug: str, numbers: list[int]) -> None:
    threading.Thread(target=publish, args=(slug, numbers), daemon=True).start()


def publish(slug: str, numbers: list[int]) -> None:
    name, label = projects.load(slug)["name"], chapter_label(numbers)
    try:
        if not projects.load(slug)["settings"]["youtube"]["channel"]:
            raise Refused("the project has no YouTube channel")
        merged = merge_videos(slug, numbers)
        _add_intro_outro(slug, merged)
        enqueue.full_video(slug, merged)
    except (Refused, RuntimeError) as e:
        telegram.notify(f"{name}: chapters {label} were not queued for YouTube - {e}.")


def _add_intro_outro(slug: str, merged: str) -> None:
    settings = projects.load(slug)["settings"]["branding"]
    if not any(settings[kind]["enabled"] for kind in branding.CLIP_KINDS):
        return
    job = Job(brand_key(slug, merged), "brand", slug, None, 0, target=merged)
    _enqueue(job)
    while job.status in ("queued", "running"):
        time.sleep(JOB_POLL_S)
    if job.status != "done":
        raise RuntimeError(f"the intro and outro render {job.status}: {job.message}")

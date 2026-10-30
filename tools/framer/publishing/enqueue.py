"""Putting videos into the upload queue: a merged video from the project's upload defaults, and the
shorts of a chapter once they are rendered."""
from __future__ import annotations

import branding
import projects
import shorts
from publishing import channels, slots, store, telegram, uploads, videos
from publishing.labels import chapter_label, clean_title, fill, part_title
from publishing.metadata import default_tags
from services.common import Refused


def series_name(project: dict) -> str:
    return project.get("series_title") or project["name"]


def _channel_of(project: dict) -> dict:
    channel = channels.get(project["settings"]["youtube"]["channel"])
    if channel is None:
        raise Refused("Choose a connected YouTube channel in this project's settings first.")
    return channel


def full_video(slug: str, merge_name: str) -> list[dict]:
    """Queue a merged video (its intro/outro version when it has one) for approval, as one item per
    part when it is too long for a single upload. Raises Refused when it cannot be queued."""
    project = projects.load(slug)
    defaults = project["settings"]["youtube"]
    channel = _channel_of(project)
    try:
        raw = branding.merge_path(slug, merge_name)
    except ValueError as e:
        raise Refused(str(e)) from e
    if not raw.is_file():
        raise Refused(f"{merge_name} not found.", 404)
    final = branding.final_path(raw)
    source = final if final.is_file() else raw
    if any(i["file"].startswith(str(source.with_suffix(""))) and i["status"] not in ("skipped", "failed", "removed")
           for i in uploads.all_items() if i["kind"] == uploads.FULL):
        raise Refused("This video is already in the upload queue.", 409)

    duration, numbers = branding.duration_of(source), branding.chapters_of(raw.name)
    parts = videos.part_count(duration)
    files = [source] if parts == 1 else videos.split(source, store.path("parts", source.stem), parts, duration)
    series, label = series_name(project), chapter_label(numbers)
    thumbnail = projects.resolve_asset_path(slug, defaults["thumbnail"])
    items = []
    for number, file in enumerate(files, start=1):
        values = {"series": series, "chapters": label, "part": number}
        items.append(uploads.add({
            "kind": uploads.FULL, "status": "waiting_approval", "project": slug, "channel": channel["id"],
            "chapters": numbers, "file": str(file), "duration_s": branding.duration_of(file),
            "title": part_title(fill(defaults["title"], **values), defaults["title"], number, parts),
            "description": fill(defaults["description"], **values), "tags": defaults["tags"],
            "thumbnail": str(thumbnail) if thumbnail else None, "playlist": series, "part": number, "parts": parts}))
    for item in items:
        telegram.announce_approval(item)
    return items


def shorts_of_chapter(slug: str, n: int) -> list[dict]:
    """Queue the rendered shorts of chapter n on the project's channel, each in its next free slot. Shorts
    of the chapter that were queued before and not sent yet are replaced. Does nothing for a project without
    a channel or with shorts uploads switched off."""
    project = projects.load(slug)
    defaults = project["settings"]["youtube"]
    channel = channels.get(defaults["channel"])
    if channel is None or not defaults["auto_shorts"]:
        return []
    output = projects.chapter_output_path(slug, n)
    uploads.remove_where(lambda i: not (i["kind"] == uploads.SHORT and i["project"] == slug and i["chapters"] == [n]
                                        and i["status"] in uploads.UNSENT))
    series = series_name(project)
    used = slots.taken(channel["id"])
    queued = []
    for short in shorts.rendered(output):
        slot = slots.next_slot(channel["id"], used)
        used.add(slot)
        queued.append(uploads.add({
            "kind": uploads.SHORT, "status": "scheduled", "project": slug, "channel": channel["id"], "chapters": [n],
            "file": str(shorts.shorts_dir(output) / short["file"]), "duration_s": short.get("duration_s"),
            "title": clean_title(short["title"]), "description": short["description"], "tags": default_tags(series),
            "script": short.get("script") or [], "model": project["settings"]["claude"]["narration_model"],
            "metadata": "pending", "publish_at": slot.isoformat(timespec="seconds"), "series": series}))
    return queued

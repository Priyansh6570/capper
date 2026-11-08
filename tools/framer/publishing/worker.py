"""The uploader: a background thread that sends the queue to YouTube. Approved full videos go up as
private; shorts go up public at their slot. Items that do not fit the day's quota wait for the next day,
failed ones are retried with growing delays, and the queue is kept in step with YouTube."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import claude_cli
import projects
from publishing import channels, metadata, pacific, quota, stats, store, sync, telegram, thumbnails, uploads
from publishing.youtube_api import (COST_UPLOAD, COST_WRITE, ApiError, AuthError, Client, QuotaExceeded,
                                    SessionExpired)

log = logging.getLogger(__name__)

TICK_S = 20
SYNC_EVERY_S = 1800
RETRY_DELAYS_S = (300, 900, 3600, 4 * 3600, 12 * 3600)
UPLOAD_NEEDS = COST_UPLOAD + 4 * COST_WRITE      # the upload, its thumbnail, the playlist's creation and insert, and a short's playlist comment
PROBE_NEEDS = 2 * COST_WRITE
CATEGORY_ENTERTAINMENT = "24"
SUMMARY_HOUR = 20
STATE_FILE = "worker.json"
_last_sync = 0.0


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _due(item: dict) -> str:
    return item.get("publish_at") or item.get("approved_at") or item["created_at"]


def _playlist_link(item: dict) -> str:
    """The project's playlist link for a short ('' for a full video or a project without one)."""
    if item["kind"] != uploads.SHORT:
        return ""
    try:
        return projects.load(item["project"])["settings"]["youtube"].get("shorts_playlist") or ""
    except Exception:  # noqa: BLE001 - a project that cannot be read has no link
        return ""


def _snippet(item: dict) -> dict:
    description = item["description"]
    if item["kind"] == uploads.SHORT and "#shorts" not in description.lower():
        description = f"{description}\n\n#Shorts".strip()
    link = _playlist_link(item)
    if link and link not in description:
        description = f"{description}\n\nFull story playlist: {link}"
    return {"title": item["title"], "description": description, "tags": item["tags"], "categoryId": CATEGORY_ENTERTAINMENT}


def _fail(item: dict, error: str, *, retry: bool) -> None:
    attempts = item["attempts"] + 1
    if retry and attempts <= len(RETRY_DELAYS_S):
        uploads.update(item["id"], status="queued", attempts=attempts, error=error, next_attempt_at=_in(RETRY_DELAYS_S[attempts - 1]))
    else:
        uploads.update(item["id"], status="failed", attempts=attempts, error=error, next_attempt_at=None)
        telegram.notify(f"Upload failed: {item['title']}\n{error}")


def _thumbnail(client: Client, item: dict) -> list[str]:
    if not item.get("thumbnail"):
        return []
    try:
        client.set_thumbnail(item["video_id"], thumbnails.jpeg_for_youtube(Path(item["thumbnail"])))
    except (ApiError, OSError) as e:
        return [f"Thumbnail not set: {e}"]
    return []


def _playlist(client: Client, channel: dict, item: dict) -> list[str]:
    """Add a full video to its series' playlist, creating the playlist the first time."""
    known, title = channel["playlists"], item["playlist"]
    try:
        playlist = known.get(title) or client.playlist_id(title)
        try:
            client.add_to_playlist(playlist, item["video_id"])
        except ApiError as e:
            if e.reason != "playlistNotFound":
                raise
            playlist = client.playlist_id(title)
            client.add_to_playlist(playlist, item["video_id"])
    except ApiError as e:
        return [f"Not added to the playlist: {e}"]
    if known.get(title) != playlist:
        channels.update(channel["id"], playlists={**known, title: playlist})
    return []


def _note_visibility(channel: dict, item: dict, requested: str, actual: str) -> None:
    """Remember whether YouTube still forces API uploads to private, and say so the first time."""
    if requested != "public":
        return
    if actual == "public" and channel["public_locked"]:
        channels.update(channel["id"], public_locked=False)
    elif actual != "public" and not channel["public_locked"]:
        channels.update(channel["id"], public_locked=True, lock_checked_at=time.time())
        telegram.notify("YouTube made an upload private although it was meant to be public: it restricts uploads of API projects "
                        "that have not passed its audit. Shorts are uploaded private for now and you can publish them in "
                        "Studio; they are switched to public by themselves once the restriction is lifted.")


def _check_quota(client_id: str) -> None:
    state = store.read(STATE_FILE, {})
    if quota.is_low(client_id) and state.get("quota_notified") != pacific.today():
        store.write(STATE_FILE, {**state, "quota_notified": pacific.today()})
        q = quota.snapshot(client_id)
        telegram.notify(f"YouTube quota is running low: {q['used']:,} of {q['limit']:,} units used today. "
                        "Uploads that do not fit wait for the reset.")


def _finish(item: dict, channel: dict, client: Client, video: dict, requested: str) -> None:
    actual = video.get("status", {}).get("privacyStatus", requested)
    locked = requested == "public" and actual != "public"
    done = uploads.update(item["id"], status="live" if actual == "public" else "private", video_id=video["id"],
                          uploaded_at=uploads.now_iso(), session_url=None, locked=locked, error=None)
    if item["kind"] == uploads.FULL:
        uploads.update(item["id"], warnings=_thumbnail(client, done) + _playlist(client, channel, done))
    _note_visibility(channel, done, requested, actual)
    if item["kind"] == uploads.SHORT and (playlist := _playlist_link(item)):
        try:
            client.comment(done["video_id"], f"Full story playlist: {playlist}")
            uploads.update(item["id"], warnings=["Playlist comment posted: pin it in Studio (the API cannot pin)."])
        except ApiError as e:
            uploads.update(item["id"], warnings=[f"Playlist comment not posted: {e}"])
    link = uploads.url_of(done)
    if item["kind"] == uploads.FULL:
        telegram.notify(f"Uploaded as private: {item['title']}\n{link}")
    elif locked:
        telegram.notify(f"Short uploaded private - publish it in Studio: {item['title']}\n{link}")
    else:
        telegram.notify(f"Short published: {item['title']}\n{link}")
    _check_quota(client.client["id"])


def _upload(item: dict, channel: dict) -> None:
    client = Client.for_channel(channel["id"])
    requested = "private" if item["kind"] == uploads.FULL else "public"
    uploads.update(item["id"], status="uploading", error=None)
    try:
        video = client.upload(
            Path(item["file"]), _snippet(item), {"privacyStatus": requested, "selfDeclaredMadeForKids": False},
            session_url=item["session_url"], on_session=lambda url: uploads.update(item["id"], session_url=url),
            progress=lambda fraction: uploads.set_progress(item["id"], fraction))
    except SessionExpired:
        uploads.update(item["id"], status="queued", session_url=None)
    except QuotaExceeded:
        uploads.update(item["id"], status="queued", next_attempt_at=_in(pacific.seconds_until_reset() + 60),
                       error="YouTube's daily quota is used up; it uploads after the reset.")
        telegram.notify("YouTube's daily quota is used up. The queue continues after the reset.")
    except AuthError as e:
        channels.update(channel["id"], needs_reconnect=True)
        _fail(item, f"Reconnect the channel in Publishing ({e}).", retry=False)
    except FileNotFoundError:
        _fail(item, "The video file is missing.", retry=False)
    except ApiError as e:
        _fail(item, str(e), retry=e.status not in (400, 403))
    except OSError as e:
        _fail(item, f"Could not read the video: {e}", retry=True)
    else:
        _finish(item, channel, client, video, requested)


def _shorts_allowed(item: dict) -> bool:
    """False for a short whose project has switched shorts uploads off (or no longer exists)."""
    if item["kind"] != uploads.SHORT:
        return True
    try:
        return projects.load(item["project"])["settings"]["youtube"]["auto_shorts"]
    except Exception:  # noqa: BLE001 - a project that cannot be read does not upload
        return False


def _release_due_shorts() -> None:
    now = datetime.now(timezone.utc)
    for item in uploads.all_items():
        if item["status"] == "scheduled" and _at(item["publish_at"]) <= now and _shorts_allowed(item):
            uploads.update(item["id"], status="queued")


def _write_metadata() -> None:
    """Have Claude write one short's title, description and tags; the plan's own stay when it cannot."""
    item = next((i for i in uploads.all_items() if i.get("metadata") == "pending" and i["status"] in ("scheduled", "queued")), None)
    if item is None:
        return
    try:
        if not claude_cli.cli_status()["ready"]:
            raise claude_cli.ClaudeError("The Claude CLI is not ready.")
        written = metadata.write(item, item["series"], item["model"])
        uploads.update(item["id"], **written, metadata="claude")
    except claude_cli.ClaudeError as e:
        log.warning("Short metadata not written by Claude (%s); the plan's title is used.", e)
        uploads.update(item["id"], metadata="plan")


def _upload_next() -> None:
    now = datetime.now(timezone.utc)
    ready = [i for i in uploads.all_items() if i["status"] == "queued" and i.get("metadata") != "pending"
             and (not i["next_attempt_at"] or _at(i["next_attempt_at"]) <= now)]
    for item in sorted(ready, key=_due):
        if not _shorts_allowed(item):
            continue
        channel = channels.get(item["channel"])
        if channel is None:
            _fail(item, "The channel is not connected any more.", retry=False)
        elif not channel["needs_reconnect"] and quota.remaining(channel["client"]["id"]) >= UPLOAD_NEEDS:
            _upload(item, channel)
            return


def _on_channel(channel: dict, task) -> list[dict]:
    try:
        return task(channel["id"])
    except AuthError:
        channels.update(channel["id"], needs_reconnect=True)
        telegram.notify(f"YouTube no longer accepts the connection to {channel['title']}: connect it again in Publishing.")
    except ApiError as e:
        log.warning("%s: %s", channel["title"], e)
    return []


def _announce_published(items: list[dict]) -> None:
    for item in items:
        telegram.notify(f"{'Short published' if item['kind'] == uploads.SHORT else 'Now public'}: {item['title']}\n{uploads.url_of(item)}")


def _housekeeping() -> None:
    global _last_sync
    if time.time() - _last_sync >= SYNC_EVERY_S:
        _last_sync = time.time()
        for channel in channels.all_channels():
            _announce_published(_on_channel(channel, sync.refresh_visibility))
    for channel in channels.all_channels():
        if sync.lock_check_due(channel) and quota.remaining(channel["client"]["id"]) >= PROBE_NEEDS:
            _announce_published(_on_channel(channel, sync.lift_lock))
    _daily_summary()


def _daily_summary() -> None:
    now, state = datetime.now(), store.read(STATE_FILE, {})
    if now.hour < SUMMARY_HOUR or state.get("last_summary") == now.date().isoformat() or not telegram.state()["paired"]:
        return
    store.write(STATE_FILE, {**state, "last_summary": now.date().isoformat()})
    yesterday = (now - timedelta(days=1)).date()
    sent = sum(1 for i in uploads.all_items() if i["uploaded_at"] and _at(i["uploaded_at"]).astimezone().date() == yesterday)
    parts = []
    for channel in channels.all_channels():
        try:
            parts.append(stats.yesterday_text(channel))
        except ApiError as e:
            parts.append(f"{channel['title']}: statistics unavailable ({e}).")
    telegram.notify("\n\n".join([f"Daily summary - {sent} upload(s) yesterday", *parts]))


def tick() -> None:
    _release_due_shorts()
    _write_metadata()
    _upload_next()
    _housekeeping()


def _loop() -> None:
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001 - the uploader must outlive any single failure
            log.exception("The upload worker failed.")
        time.sleep(TICK_S)


def start() -> None:
    for item in uploads.all_items():
        if item["status"] == "uploading":
            uploads.update(item["id"], status="queued")
    threading.Thread(target=_loop, daemon=True).start()

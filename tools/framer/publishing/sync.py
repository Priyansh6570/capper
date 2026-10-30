"""Keeping the queue in step with YouTube: videos made public in Studio, videos deleted there, and
shorts that YouTube forced to private until the API project passes its audit."""
from __future__ import annotations

import time

from publishing import channels, uploads
from publishing.youtube_api import ApiError, Client, QuotaExceeded

LOCK_CHECK_EVERY_S = 6 * 3600


def refresh_visibility(channel_id: str) -> list[dict]:
    """Compare the channel's private uploads with YouTube. Returns the items that are public now."""
    private = [i for i in uploads.all_items() if i["channel"] == channel_id and i["status"] == "private" and i["video_id"]]
    if not private:
        return []
    found = {v["id"]: v for v in Client.for_channel(channel_id).videos([i["video_id"] for i in private], "status")}
    published = []
    for item in private:
        video = found.get(item["video_id"])
        if video is None:
            uploads.update(item["id"], status="removed", error="Deleted from YouTube.")
        elif video["status"]["privacyStatus"] == "public":
            published.append(uploads.update(item["id"], status="live", locked=False))
    return published


def stuck_shorts(channel_id: str) -> list[dict]:
    """Shorts uploaded private only because YouTube locks API uploads of an unaudited project, oldest first."""
    return sorted((i for i in uploads.all_items() if i["kind"] == uploads.SHORT and i["channel"] == channel_id
                   and i["locked"] and i["status"] == "private" and i["video_id"]), key=lambda i: i["uploaded_at"])


def lock_check_due(channel: dict) -> bool:
    """A channel known to be locked is probed now and then; one that was released finishes its backlog at once."""
    if not stuck_shorts(channel["id"]):
        return False
    return not channel["public_locked"] or time.time() - channel.get("lock_checked_at", 0) >= LOCK_CHECK_EVERY_S


def lift_lock(channel_id: str) -> list[dict]:
    """Try to make the stuck shorts public. The first one is the probe: when YouTube refuses, the restriction
    is still on; when it works the restriction is over and the rest follow. Returns the shorts now public."""
    channels.update(channel_id, lock_checked_at=time.time())
    client, published = Client.for_channel(channel_id), []
    for item in stuck_shorts(channel_id):
        try:
            if client.set_privacy(item["video_id"], "public") != "public":
                break
        except QuotaExceeded:
            raise
        except ApiError:
            if not published:
                break
            raise
        published.append(uploads.update(item["id"], status="live", locked=False))
        channels.update(channel_id, public_locked=False)
    return published

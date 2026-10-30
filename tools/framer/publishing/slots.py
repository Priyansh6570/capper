"""When a channel's shorts go out: N a day, spread evenly between two hours of the local day."""
from __future__ import annotations

from datetime import date, datetime, time as clock, timedelta, timezone

from publishing import channels, uploads

LOOKAHEAD_DAYS = 800
MIN_LEAD = timedelta(minutes=1)


def clamp_schedule(raw: dict) -> dict:
    """A valid schedule from user input: 1-48 a day, between two different hours of the day."""
    per_day = max(1, min(48, int(raw.get("per_day") or 1)))
    first = max(0, min(23, int(raw.get("from_hour") if raw.get("from_hour") not in (None, "") else 9)))
    last = max(first + 1, min(24, int(raw.get("to_hour") or 21)))
    return {"per_day": per_day, "from_hour": first, "to_hour": last}


def slots_of(schedule: dict, day: date) -> list[datetime]:
    span = schedule["to_hour"] - schedule["from_hour"]
    start = datetime.combine(day, clock.min)
    return [(start + timedelta(seconds=round(3600 * (schedule["from_hour"] + span * (i + 0.5) / schedule["per_day"])))).astimezone(timezone.utc)
            for i in range(schedule["per_day"])]


def taken(channel_id: str) -> set[datetime]:
    return {datetime.fromisoformat(i["publish_at"]) for i in uploads.all_items()
            if i["kind"] == uploads.SHORT and i["channel"] == channel_id and i.get("publish_at")
            and i["status"] in ("scheduled", "queued", "uploading")}


def next_slot(channel_id: str, used: set[datetime]) -> datetime:
    """The first slot of the channel's shorts schedule that is still ahead and not in `used`."""
    schedule = (channels.get(channel_id) or {}).get("shorts") or channels.DEFAULT_SHORTS
    earliest = datetime.now(timezone.utc) + MIN_LEAD
    today = datetime.now().date()
    for offset in range(LOOKAHEAD_DAYS):
        for slot in slots_of(schedule, today + timedelta(days=offset)):
            if slot >= earliest and slot not in used:
                return slot
    raise ValueError("The shorts schedule has no free slot.")


def reschedule(channel_id: str) -> None:
    """Give every short still waiting for its slot a new one after the schedule changed, keeping their order."""
    waiting = sorted((i for i in uploads.all_items() if i["kind"] == uploads.SHORT and i["channel"] == channel_id
                      and i["status"] == "scheduled"), key=lambda i: i["publish_at"])
    used = taken(channel_id) - {datetime.fromisoformat(i["publish_at"]) for i in waiting}
    for item in waiting:
        slot = next_slot(channel_id, used)
        used.add(slot)
        uploads.update(item["id"], publish_at=slot.isoformat(timespec="seconds"))

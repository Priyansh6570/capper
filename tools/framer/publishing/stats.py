"""Channel analytics and the views of recent uploads, as the text the Telegram bot sends."""
from __future__ import annotations

from datetime import timedelta

from publishing import pacific, uploads
from publishing.youtube_api import Client

METRICS = "views,estimatedMinutesWatched,subscribersGained"
TOP_VIDEOS = 5
RECENT_VIDEOS = 8


def _totals(client: Client, first, last) -> dict:
    rows = client.report(first.isoformat(), last.isoformat(), METRICS)
    return rows[0] if rows else {"views": 0, "estimatedMinutesWatched": 0, "subscribersGained": 0}


def _summary(totals: dict) -> str:
    return (f"Views: {totals['views']:,}\nWatch time: {totals['estimatedMinutesWatched'] / 60:,.1f} h\n"
            f"Subscribers gained: {totals['subscribersGained']:+,}")


def _titles(client: Client, ids: list[str]) -> dict[str, str]:
    return {v["id"]: v["snippet"]["title"] for v in client.videos(ids, "snippet")} if ids else {}


def week_text(channel: dict) -> str:
    """The last 7 full days of the channel: totals, its top videos, and the views of each recent upload."""
    client, last = Client.for_channel(channel["id"]), pacific.now().date() - timedelta(days=1)
    first = last - timedelta(days=6)
    top = client.report(first.isoformat(), last.isoformat(), "views", dimensions="video", sort="-views", limit=TOP_VIDEOS)
    titles = _titles(client, [row["video"] for row in top])
    lines = [f"{channel['title']} - last 7 days", _summary(_totals(client, first, last))]
    if top:
        lines += ["", "Top videos"] + [f"{n}. {titles.get(row['video'], row['video'])} - {row['views']:,} views"
                                       for n, row in enumerate(top, start=1)]
    return "\n".join(lines + ["", recent_views_text(client, channel["id"])])


def yesterday_text(channel: dict) -> str:
    client, day = Client.for_channel(channel["id"]), pacific.now().date() - timedelta(days=1)
    return f"{channel['title']} - yesterday\n{_summary(_totals(client, day, day))}"


def recent_views_text(client: Client, channel_id: str) -> str:
    recent = sorted((i for i in uploads.all_items() if i["channel"] == channel_id and i["video_id"]
                     and i["status"] in uploads.UPLOADED), key=lambda i: i["uploaded_at"], reverse=True)[:RECENT_VIDEOS]
    if not recent:
        return "No uploads from ReCapper yet."
    views = {v["id"]: v["statistics"].get("viewCount", "0") for v in client.videos([i["video_id"] for i in recent], "statistics")}
    return "Recent uploads\n" + "\n".join(f"- {i['title']}: {int(views.get(i['video_id'], 0)):,} views" for i in recent)

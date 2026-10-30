"""Pacific time without a time zone database: YouTube's API quota resets at midnight there."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7, weeks=n - 1)


def offset(moment: datetime) -> timedelta:
    """UTC offset in the US Pacific zone at `moment`: daylight time runs from 2am on the second
    Sunday of March to 2am on the first Sunday of November."""
    utc = moment.astimezone(timezone.utc)
    start = datetime.combine(_nth_sunday(utc.year, 3, 2), datetime.min.time(), timezone.utc) + timedelta(hours=10)
    end = datetime.combine(_nth_sunday(utc.year, 11, 1), datetime.min.time(), timezone.utc) + timedelta(hours=9)
    return timedelta(hours=-7) if start <= utc < end else timedelta(hours=-8)


def now() -> datetime:
    current = datetime.now(timezone.utc)
    return current + offset(current)


def today() -> str:
    return now().date().isoformat()


def seconds_until_reset() -> float:
    current = now()
    midnight = datetime.combine(current.date() + timedelta(days=1), datetime.min.time(), current.tzinfo)
    return (midnight - current).total_seconds()

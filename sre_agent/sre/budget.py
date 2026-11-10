"""Investigation budget: a flood of distinct errors must not become a flood of model calls.

Every new issue is investigated with several model calls. Without a ceiling, a bad deploy that produces hundreds
of slightly different messages means hundreds of investigations: a large bill, provider rate limits (which then
make the analysis fall back to the template), and a queue that never drains.

    INVESTIGATIONS_PER_HOUR (30)   INVESTIGATIONS_PER_DAY (300)   0 = no limit
    CRITICAL signals may use up to 3x the hourly and daily limits, so a flood of noise cannot starve a real emergency.

A signal over budget is not lost: it is marked `throttled`, one alert says so, and it is picked up again by the
reconcile pass as soon as the window frees up. The investigation counts are stored in the database, so a restart
loop cannot reset them.
"""
import os
from datetime import datetime, timedelta, timezone

from sre_agent.core.db import count_items, create_item

KIND = "investigation_run"
CRITICAL_FACTOR = 3


def _cap(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, default)))
    except ValueError:
        return default


def hourly_cap() -> int:
    return _cap("INVESTIGATIONS_PER_HOUR", 30)


def daily_cap() -> int:
    return _cap("INVESTIGATIONS_PER_DAY", 300)


def usage(now: datetime | None = None) -> tuple[int, int]:
    """(investigations started in the last hour, in the last 24 hours)."""
    now = now or datetime.now(timezone.utc)
    return (count_items(KIND, since=now - timedelta(hours=1)), count_items(KIND, since=now - timedelta(hours=24)))


def allow(severity: str | None = None, now: datetime | None = None) -> bool:
    factor = CRITICAL_FACTOR if str(severity).upper() == "CRITICAL" else 1
    hour, day = usage(now)
    hcap, dcap = hourly_cap(), daily_cap()
    return not ((hcap and hour >= hcap * factor) or (dcap and day >= dcap * factor))


def record(signal_id: str) -> None:
    create_item(KIND, {"signal_id": signal_id})


def describe(now: datetime | None = None) -> str:
    hour, day = usage(now)
    return f"{hour} investigation(s) in the last hour (limit {hourly_cap() or 'none'}), {day} in 24 h (limit {daily_cap() or 'none'})"

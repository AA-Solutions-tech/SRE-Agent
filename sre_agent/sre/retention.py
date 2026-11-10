"""Keep the agent's database from growing forever.

Raw log evidence is stored on every signal, and every lookup scans more than it needs when nothing is ever
removed. Daily, this removes only finished, unreferenced records:

    signals that were handled or only counted          RETENTION_DAYS_SIGNALS (30)
    coding results, fix-failed cards, budget markers   RETENTION_DAYS_RESULTS (60; budget markers 3 days)
    issues that are closed (fixed / ignored / rejected) with no sighting for  RETENTION_DAYS_ISSUES (180)

Never removed: signals still waiting (new, held, throttled, handoff_failed), open issues, investigations and
approval records (issues and fix records point at them). 0 disables a rule.
"""
import os
from datetime import datetime, timedelta, timezone

from sre_agent.core.db import delete_item, list_items, prune
from sre_agent.core.sre_log import get_logger, step
from sre_agent.sre import budget, issues

logger = get_logger("retention")
FINISHED_SIGNAL_STATUSES = ("handed_off", "counted", "handoff_gave_up")
CLOSED_ISSUE_STATES = ("fixed", "ignored", "rejected")


def _days(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, default)))
    except ValueError:
        return default


def run(now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    removed = {"signals": 0, "results": 0, "budget": 0, "issues": 0}
    days = _days("RETENTION_DAYS_SIGNALS", 30)
    if days:
        removed["signals"] = prune("signal", timedelta(days=days), FINISHED_SIGNAL_STATUSES)
    days = _days("RETENTION_DAYS_RESULTS", 60)
    if days:
        removed["results"] = prune("coding_result", timedelta(days=days)) + prune("fix_failed", timedelta(days=days))
    removed["budget"] = prune(budget.KIND, timedelta(days=3))
    days = _days("RETENTION_DAYS_ISSUES", 180)
    if days:
        cutoff = now - timedelta(days=days)
        for item in list_items(kind=issues.KIND):
            d = item["data"]
            seen = issues._parse(d.get("last_seen"))
            if d.get("state") in CLOSED_ISSUE_STATES and seen and seen < cutoff:
                delete_item(item["id"])
                removed["issues"] += 1
    if any(removed.values()):
        step(logger, "retention.01.run", "old records removed", **removed)
    return removed

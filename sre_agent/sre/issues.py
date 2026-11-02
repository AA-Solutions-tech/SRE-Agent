"""Issue tracking: one record per distinct error, so repeats never mean repeated cards.

A fingerprint = service + the error message with ids, numbers and timestamps removed. Every
occurrence of the same fingerprint belongs to ONE issue (kind "issue") with a lifecycle:

    open ---approve--> fix_pending ---quiet window--> fixed ---recurs--> open (regression)
      |                    ^  reminders while still failing
      +--reject--> rejected ---snooze over, recurs--> open
      +--"I'll fix this"--> developer_fix (never coded; reminders until quiet) ---quiet--> fixed

record_occurrence() decides what a new occurrence means: investigate (send a card) or just count.
All writes are serialized by one lock (monitor thread, approval clicks and the sweeper all call in).
"""
import hashlib
import os
import re
import threading
from datetime import datetime, timedelta, timezone

from sre_agent.core.db import create_item, get_item, list_items, update_item
from sre_agent.core.sre_log import get_logger, step

logger = get_logger("issues")
KIND = "issue"
ACTIVE = ("open", "fix_pending", "developer_fix")
REMINDED = ("fix_pending", "developer_fix")
RETRY_UNINVESTIGATED = timedelta(minutes=10)
_lock = threading.RLock()

_ID_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_NUM_RE = re.compile(r"\d+")
_SPACE_RE = re.compile(r"\s+")


def _minutes(name: str, default: float) -> timedelta:
    try:
        return timedelta(minutes=float(os.getenv(name, default)))
    except ValueError:
        return timedelta(minutes=default)


def quiet_window() -> timedelta:
    return _minutes("ISSUE_QUIET_MINUTES", 30)


def snooze_window() -> timedelta:
    return timedelta(hours=_hours("ISSUE_SNOOZE_HOURS", 24))


def remind_interval() -> timedelta:
    return timedelta(hours=_hours("ISSUE_REMIND_HOURS", 24))


def _hours(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def normalize(text: str) -> str:
    """Message with ids, numbers and spacing removed, so equal bugs normalize to equal strings."""
    text = _ID_RE.sub("<id>", text.lower())
    text = _NUM_RE.sub("<n>", text)
    return _SPACE_RE.sub(" ", text).strip()[:200]


def fingerprint(service: str, message: str) -> str:
    return hashlib.sha1(f"{service}|{normalize(message)}".encode("utf-8", "replace")).hexdigest()[:12]


def find_by_fingerprint(fp: str) -> dict | None:
    return next((i for i in list_items(kind=KIND) if i["data"].get("fingerprint") == fp), None)


def record_occurrence(signal: dict, signal_id: str) -> tuple[str, str]:
    """Register one occurrence. Returns (issue_id, action) where action is
    'investigate' (new issue, regression or snooze over) or 'count' (already known: no new card)."""
    fp = signal.get("fingerprint") or fingerprint(signal.get("service", ""), signal.get("error", ""))
    now = _now()
    with _lock:
        issue = find_by_fingerprint(fp)
        if issue is None:
            issue_id = create_item(KIND, {
                "fingerprint": fp, "service": signal.get("service"), "error": signal.get("error"),
                "severity": signal.get("severity"), "state": "open", "count": 1, "first_seen": _iso(now),
                "last_seen": _iso(now), "signal_ids": [signal_id], "investigation_id": None,
                "regressions": 0, "state_since": _iso(now),
            })
            step(logger, "issues.01.new", "new issue", issue_id=issue_id, service=signal.get("service"),
                 error=signal.get("error"), fingerprint=fp)
            return issue_id, "investigate"
        d = issue["data"]
        state, action = d.get("state", "open"), "count"
        changes = {"count": d.get("count", 0) + 1, "last_seen": _iso(now),
                   "signal_ids": (d.get("signal_ids") or [])[-49:] + [signal_id]}
        if state == "fixed":
            action = "investigate"
            changes.update(state="open", regressions=d.get("regressions", 0) + 1,
                           state_since=_iso(now), investigation_id=None)
        elif state == "rejected" and now >= (_parse(d.get("snooze_until")) or now):
            action = "investigate"
            changes.update(state="open", state_since=_iso(now), investigation_id=None)
        elif state == "open" and not d.get("investigation_id") and \
                now - (_parse(d.get("state_since")) or now) > RETRY_UNINVESTIGATED:
            action = "investigate"  # the first attempt never produced a card
            changes["state_since"] = _iso(now)
        update_item(issue["id"], **changes)
        step(logger, "issues.02.occurrence", "known issue seen again", issue_id=issue["id"], state=state,
             count=changes["count"], action=action, level="info" if action == "count" else "warning")
        return issue["id"], action


def get_issue(issue_id: str | None) -> dict | None:
    item = get_item(issue_id) if issue_id else None
    return item if item and item["kind"] == KIND else None


def _set_state(issue_id: str | None, state: str, **extra) -> None:
    if not get_issue(issue_id):
        return
    with _lock:
        update_item(issue_id, state=state, state_since=_iso(_now()), last_reminder=None, **extra)
    step(logger, "issues.03.state", "issue state changed", issue_id=issue_id, state=state)


def attach_investigation(issue_id: str | None, investigation_id: str) -> None:
    if get_issue(issue_id):
        with _lock:
            update_item(issue_id, investigation_id=investigation_id)


def mark_fix_pending(issue_id: str | None) -> None:
    _set_state(issue_id, "fix_pending", approved_at=_iso(_now()))


def mark_rejected(issue_id: str | None) -> None:
    _set_state(issue_id, "rejected", snooze_until=_iso(_now() + snooze_window()))


def mark_developer_fix(issue_id: str | None) -> None:
    _set_state(issue_id, "developer_fix", claimed_at=_iso(_now()))


def mark_fixed(issue_id: str | None) -> None:
    _set_state(issue_id, "fixed", fixed_at=_iso(_now()))


def sweep(now: datetime | None = None) -> list[dict]:
    """Close issues that stayed quiet for the quiet window; return the issues due a reminder.

    A reminder is due for fix_pending / developer_fix issues that are still failing (seen inside the
    quiet window) once REMIND interval has passed since the state began or the last reminder."""
    now = now or _now()
    due: list[dict] = []
    for issue in list_items(kind=KIND):
        d = issue["data"]
        if d.get("state") not in REMINDED:
            continue
        last_seen = _parse(d.get("last_seen")) or now
        if now - last_seen > quiet_window():
            mark_fixed(issue["id"])
            continue
        since = _parse(d.get("last_reminder")) or _parse(d.get("state_since")) or now
        if now - since >= remind_interval():
            with _lock:
                update_item(issue["id"], last_reminder=_iso(now))
            due.append({**d, "id": issue["id"]})
    return due


def describe(d: dict) -> str:
    """One-line occurrence summary for cards and reminders."""
    return (f"seen {d.get('count', 1)} time(s), first {str(d.get('first_seen', '?'))[:16]}, "
            f"latest {str(d.get('last_seen', '?'))[:16]}"
            + (f", {d['regressions']} regression(s)" if d.get("regressions") else ""))

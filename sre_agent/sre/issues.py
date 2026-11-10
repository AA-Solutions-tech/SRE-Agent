"""Issue tracking: one record per distinct error, so repeats never mean repeated cards.

A fingerprint = service + the error message with ids, numbers and timestamps removed. Every
occurrence of the same fingerprint belongs to ONE issue (kind "issue") with a lifecycle:

    watching --enough repeats--> open   (rate limits / flaky upstreams: counted quietly until they persist)
    open ---no answer to the card--> reminders every ISSUE_UNANSWERED_HOURS (+ escalation) ---quiet--> fixed
    open ---approve--> fix_pending ---quiet window--> fixed ---recurs--> open (regression)
      |                    ^  reminders while still failing
      +--reject--> rejected ---snooze over, recurs--> open
      +--"I'll fix this"--> developer_fix (never coded; reminders until quiet) ---quiet--> fixed
      +--ignore--> ignored (counted silently) ---more than ISSUE_IGNORE_THRESHOLD repeats--> open

record_occurrence() decides what a new occurrence means: investigate (send a card) or just count.
All writes are serialized by one lock (monitor thread, approval clicks and the sweeper all call in).
"""
import hashlib
import os
import re
import threading
from datetime import datetime, timedelta, timezone

from sre_agent.core.db import create_item, find_by_field, get_item, list_items, update_item
from sre_agent.core.sre_log import get_logger, step
from sre_agent.sre.classify import WATCH_CATEGORIES

logger = get_logger("issues")
KIND = "issue"
ACTIVE = ("open", "fix_pending", "developer_fix")
REMINDED = ("fix_pending", "developer_fix")
RETRY_UNINVESTIGATED = timedelta(minutes=10)
_lock = threading.RLock()

_URL_RE = re.compile(r"https?://\S+")
_EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_QUOTED_RE = re.compile(r"""(["'`])[^"'`\n]{1,80}\1""")
_ABS_PATH_RE = re.compile(r"(?<![\w/:.<>])(?:/[\w.\-~@%+]+)+/?")
_WIN_PATH_RE = re.compile(r"\b[a-z]:[\\/][\w.\-~@%+\\/]+")
_REL_PATH_RE = re.compile(r"\b[\w.\-]+(?:/[\w.\-]+)*/[\w\-]+\.\w{1,6}\b")
_FILE_RE = re.compile(r"\b[\w\-]+\.(?:ico|php\d?|html?|js|mjs|css|map|png|jpe?g|gif|svg|webp|json|xml|txt|asp|aspx|jsp|env|"
                      r"git|sql|bak|zip|gz|tar|ya?ml|conf|ini|log|py|rb|sh|woff2?|ttf|pdf|csv)\b")
_HOST_RE = re.compile(r"\b(?:[a-z0-9\-]+\.)+(?:com|net|org|io|dev|co|in|app|ai|cloud|info|biz|xyz|local|internal|gov|edu)\b")
_CODE_RE = re.compile(r"\b(?=[a-z]*\d)(?=\d*[a-z])[a-z0-9]{2,}\b")  # ab12, e11000, s3: letters and digits mixed
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


def watch_threshold() -> int:
    """Occurrences of a transient error (rate limit, flaky upstream) inside the watch window that make it a card."""
    try:
        return max(1, int(os.getenv("WATCH_THRESHOLD", 5)))
    except ValueError:
        return 5


def watch_window() -> timedelta:
    return _minutes("WATCH_WINDOW_MINUTES", 10)


def deploy_grace() -> timedelta:
    """After a fix is merged, errors seen inside this window are the old version still running, not a failed fix."""
    return _minutes("VERIFY_DEPLOY_GRACE_MINUTES", 15)


def unanswered_interval() -> timedelta:
    """How long a card may sit unanswered before it is sent again."""
    return timedelta(hours=_hours("ISSUE_UNANSWERED_HOURS", 4))


def escalate_after() -> timedelta | None:
    """Unanswered this long: also tell a second person/channel (ESCALATE_AFTER_HOURS, 0 = never)."""
    hours = _hours("ESCALATE_AFTER_HOURS", 0)
    return timedelta(hours=hours) if hours > 0 else None


def low_escalate_count() -> int:
    """A low-priority issue seen this many times becomes a normal card (0 = never)."""
    try:
        return max(0, int(os.getenv("LOW_ESCALATE_COUNT", 200)))
    except ValueError:
        return 200


def ignore_threshold() -> int:
    """Occurrences after Ignore that bring the issue back as a normal card."""
    try:
        return max(1, int(os.getenv("ISSUE_IGNORE_THRESHOLD", 15)))
    except ValueError:
        return 15


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
    """The message with everything that varies between occurrences replaced by a placeholder, so one bug always
    normalizes to one string: ids, numbers, emails, URLs, IPs, UUIDs, quoted values, paths, file names, hosts and
    short codes. FINGERPRINT_STRICT=true keeps only the first group (distinct quoted names, paths and files then
    stay distinct issues)."""
    text = _URL_RE.sub("<url>", text.lower())
    text = _EMAIL_RE.sub("<email>", text)
    text = _UUID_RE.sub("<id>", text)
    text = _IP_RE.sub("<ip>", text)
    if os.getenv("FINGERPRINT_STRICT", "false").strip().lower() != "true":
        for pattern, token in ((_QUOTED_RE, "<q>"), (_ABS_PATH_RE, "<path>"), (_WIN_PATH_RE, "<path>"),
                               (_REL_PATH_RE, "<path>"), (_FILE_RE, "<file>"), (_HOST_RE, "<host>")):
            text = pattern.sub(token, text)
    text = _ID_RE.sub("<id>", text)
    if os.getenv("FINGERPRINT_STRICT", "false").strip().lower() != "true":
        text = _CODE_RE.sub("<code>", text)
    text = _NUM_RE.sub("<n>", text)
    return _SPACE_RE.sub(" ", text).strip()[:200]


def fingerprint(service: str, message: str) -> str:
    return hashlib.sha1(f"{service}|{normalize(message)}".encode("utf-8", "replace")).hexdigest()[:12]


def find_by_fingerprint(fp: str) -> dict | None:
    return find_by_field(KIND, "fingerprint", fp)  # indexed: this runs for every error line


def _hits(signal: dict) -> int:
    """How many log lines this signal stands for (a burst of 30 throttle errors is 30 hits, not 1)."""
    return max(1, len(signal.get("evidence") or []))


def _watch(events: list, hits: int, now: datetime) -> tuple[list, int]:
    """Add `hits` to the recent events; returns (events inside the window, their total)."""
    cutoff = now - watch_window()
    kept = [e for e in events if (_parse(e[0]) or now) >= cutoff] + [[_iso(now), hits]]
    return kept, sum(h for _, h in kept)


def record_occurrence(signal: dict, signal_id: str) -> tuple[str, str]:
    """Register one occurrence. Returns (issue_id, action) where action is
    'investigate' (new issue, regression or snooze over) or 'count' (already known: no new card)."""
    fp = signal.get("fingerprint") or fingerprint(signal.get("service", ""), signal.get("error", ""))
    now = _now()
    watched = signal.get("category") in WATCH_CATEGORIES
    with _lock:
        issue = find_by_fingerprint(fp)
        if issue is None:
            events, total = _watch([], _hits(signal), now) if watched else ([], 0)
            state = "watching" if watched and total < watch_threshold() else "open"
            if signal.get("severity") == "LOW":  # noise-level: counted quietly, reported in the digest, no card
                state = "low"
            issue_id = create_item(KIND, {
                "fingerprint": fp, "service": signal.get("service"), "error": signal.get("error"),
                "severity": signal.get("severity"), "category": signal.get("category"), "state": state,
                "count": 1, "first_seen": _iso(now), "last_seen": _iso(now), "signal_ids": [signal_id],
                "investigation_id": None, "regressions": 0, "state_since": _iso(now), "watch_events": events,
            })
            step(logger, "issues.01.new", {"open": "new issue", "low": "new issue, low priority"}.get(state, "new issue, watching"),
                 issue_id=issue_id, service=signal.get("service"), error=signal.get("error"),
                 fingerprint=fp, category=signal.get("category"))
            return issue_id, "count" if state in ("watching", "low") else "investigate"
        d = issue["data"]
        state, action = d.get("state", "open"), "count"
        changes = {"count": d.get("count", 0) + 1, "last_seen": _iso(now),
                   "signal_ids": (d.get("signal_ids") or [])[-49:] + [signal_id]}
        if state == "watching":  # transient kind: only a card once it keeps happening
            events, total = _watch(d.get("watch_events") or [], _hits(signal), now)
            changes["watch_events"] = events
            if total >= watch_threshold():
                action = "investigate"
                changes.update(state="open", state_since=_iso(now), investigation_id=None)
        elif state == "low":  # stays quiet until it is frequent enough to matter
            limit = low_escalate_count()
            if limit and changes["count"] >= limit and signal.get("severity") != "LOW":
                action = "investigate"
                changes.update(state="open", state_since=_iso(now), investigation_id=None)
            elif limit and changes["count"] >= limit:
                action = "investigate"
                changes.update(state="open", state_since=_iso(now), investigation_id=None, severity="MEDIUM")
        elif state == "fixed" and signal.get("severity") == "LOW":  # came back, but only as noise
            changes.update(state="low", state_since=_iso(now))
        elif state == "fixed":
            action = "investigate"
            fixed_at = _parse(d.get("fixed_at"))
            changes.update(state="open", regressions=d.get("regressions", 0) + 1,
                           state_since=_iso(now), investigation_id=None,
                           reopened_after_fix_min=int((now - fixed_at).total_seconds() // 60) if fixed_at else None)
        elif (state == "fix_pending" and d.get("merged_at")
              and now - _parse(d["merged_at"]) > deploy_grace()):  # merged and deployed, yet it is still happening
            action = "investigate"
            changes.update(state="open", regressions=d.get("regressions", 0) + 1, state_since=_iso(now),
                           investigation_id=None, reopened_after_merge_min=int((now - _parse(d["merged_at"])).total_seconds() // 60),
                           merged_at=None, pr_number=None, pr_url=None, pr_state=None)
        elif state == "rejected" and now >= (_parse(d.get("snooze_until")) or now):
            action = "investigate"
            changes.update(state="open", state_since=_iso(now), investigation_id=None)
        elif state == "ignored" and changes["count"] - d.get("ignored_at_count", 0) > ignore_threshold():
            action = "investigate"  # too noisy to stay ignored: back to a normal card
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


def mark_ignored(issue_id: str | None) -> None:
    issue = get_issue(issue_id)
    if issue:
        _set_state(issue_id, "ignored", ignored_at_count=issue["data"].get("count", 0))


def mark_developer_fix(issue_id: str | None) -> None:
    _set_state(issue_id, "developer_fix", claimed_at=_iso(_now()))


def mark_fix_failed(issue_id: str | None, reason: str) -> None:
    """The coding run produced no usable fix: back to open (so it is reminded about), with the reason on record."""
    _set_state(issue_id, "open", fix_failed_reason=str(reason)[:300], last_card_reminder=None, escalated_at=None,
               pr_number=None, pr_url=None, pr_state=None, merged_at=None)


def mark_merged(issue_id: str | None, merged_at: datetime | None = None) -> None:
    """The fix's pull request was merged: from now on the issue must stay quiet to count as fixed."""
    if get_issue(issue_id):
        with _lock:
            update_item(issue_id, merged_at=_iso(merged_at or _now()), pr_state="merged")
        step(logger, "issues.04.merged", "fix merged, verifying", issue_id=issue_id)


def mark_pr_closed(issue_id: str | None) -> None:
    """The pull request was closed without merging: the fix did not land."""
    mark_fix_failed(issue_id, "the pull request was closed without being merged")


def mark_fixed(issue_id: str | None) -> None:
    _set_state(issue_id, "fixed", fixed_at=_iso(_now()), reopened_after_fix_min=None)


def low_digest() -> list[dict]:
    """Low-priority issues seen since the last digest, busiest first (each is marked as reported)."""
    out = []
    for item in list_items(kind=KIND):
        d = item["data"]
        new = d.get("count", 0) - d.get("digest_count", 0)
        if d.get("state") == "low" and new > 0:
            out.append({**d, "id": item["id"], "new": new})
            update_item(item["id"], digest_count=d.get("count", 0))
    return sorted(out, key=lambda i: -i["new"])


def record_delivery(issue_id: str | None, deliveries) -> None:
    """Keep what happened when the card was sent (per channel: ok, HTTP status, error), so delivery can be proven."""
    if isinstance(deliveries, list) and get_issue(issue_id):
        update_item(issue_id, last_delivery=deliveries)


def sweep_all(now: datetime | None = None) -> dict[str, list[dict]]:
    """One housekeeping pass. Returns what needs a message:

      reminders   fix_pending / developer_fix issues still failing, once REMIND interval has passed
      unanswered  open issues whose card nobody answered (escalate=True when ESCALATE_AFTER_HOURS has passed)
      verified    issues this pass closed because they stayed quiet (was = the state they were in)

    Issues that only ever sat in `watching` are closed silently."""
    now = now or _now()
    out: dict[str, list[dict]] = {"reminders": [], "unanswered": [], "verified": []}
    for issue in list_items(kind=KIND):
        d, issue_id = issue["data"], issue["id"]
        state = d.get("state")
        last_seen = _parse(d.get("last_seen")) or now
        quiet = now - last_seen > quiet_window()
        if state == "watching":  # never became a card and went quiet: close it silently
            if quiet:
                mark_fixed(issue_id)
            continue
        if state == "open" and d.get("investigation_id"):  # a card went out and nobody answered
            if quiet:  # it stopped on its own
                mark_fixed(issue_id)
                out["verified"].append({**d, "id": issue_id, "was": "open"})
                continue
            since = _parse(d.get("last_card_reminder")) or _parse(d.get("state_since")) or now
            if now - since >= unanswered_interval():
                esc = escalate_after()
                escalate = bool(esc and not d.get("escalated_at") and
                                now - (_parse(d.get("state_since")) or now) >= esc)
                with _lock:
                    update_item(issue_id, last_card_reminder=_iso(now), **({"escalated_at": _iso(now)} if escalate else {}))
                waited = (now - (_parse(d.get("state_since")) or now)).total_seconds() / 3600
                out["unanswered"].append({**d, "id": issue_id, "waiting_hours": waited, "escalate": escalate})
            continue
        if state not in REMINDED:
            continue
        awaiting_merge = state == "fix_pending" and bool(d.get("pr_number")) and not d.get("merged_at")
        merged_at = _parse(d.get("merged_at"))
        if merged_at and now - merged_at <= quiet_window():
            continue  # merged recently: it must stay quiet for a full window AFTER the merge
        if quiet and not awaiting_merge:
            mark_fixed(issue_id)
            out["verified"].append({**d, "id": issue_id, "was": state})
            continue
        since = _parse(d.get("last_reminder")) or _parse(d.get("state_since")) or now
        if now - since >= remind_interval():
            with _lock:
                update_item(issue_id, last_reminder=_iso(now))
            out["reminders"].append({**d, "id": issue_id})
    return out


def sweep(now: datetime | None = None) -> list[dict]:
    """Housekeeping pass; returns only the issues due a still-failing reminder (see sweep_all for the rest)."""
    return sweep_all(now)["reminders"]


def describe(d: dict) -> str:
    """One-line occurrence summary for cards and reminders."""
    return (f"seen {d.get('count', 1)} time(s), first {str(d.get('first_seen', '?'))[:16]}, "
            f"latest {str(d.get('last_seen', '?'))[:16]}"
            + (f", {d['regressions']} regression(s)" if d.get("regressions") else "")
            + (f"; it came back {d['reopened_after_fix_min']} min after being marked fixed, so that fix did not hold"
               if d.get("reopened_after_fix_min") is not None else "")
            + (f"; it came back {d['reopened_after_merge_min']} min after the fix was merged, so the fix did not work"
               if d.get("reopened_after_merge_min") is not None else "")
            + (f"; the automatic fix failed: {d['fix_failed_reason']}" if d.get("fix_failed_reason") else ""))

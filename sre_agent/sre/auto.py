"""Auto mode: SRE_AUTO_RUN=true makes the server run the whole loop by itself.

    monitor (watch thread) --signal--> supervisor --> incident (card)
    human clicks Approve --> coding (background thread)

Each agent is still called through its public run() only (load_agent), so
deleting an agent folder just shrinks the chain. Off by default.
"""
import itertools
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

from sre_agent.core import paging
from sre_agent.core.config import settings
from sre_agent.core.db import get_item, list_items, update_item
from sre_agent.core.notify import notify
from sre_agent.core.sre_log import bind_trace, get_logger, step
from sre_agent.sre import budget, fixfail, health, ignorelist, issues, retention, revision, schedules
from sre_agent.sre import severity as severity_rules

logger = get_logger("auto")
_started = False

# ---- investigation queue: the watcher only enqueues; workers do the slow model work ----
_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
_queue: queue.PriorityQueue = queue.PriorityQueue()
_queued: set[str] = set()  # queued or being processed: a signal is never worked on twice at once
_queued_lock = threading.Lock()
_seq = itertools.count()
_stop = threading.Event()
_workers: list[threading.Thread] = []


def enabled() -> bool:
    return os.getenv("SRE_AUTO_RUN", "false").strip().lower() == "true"


def _max_attempts() -> int:
    try:
        return max(1, int(os.getenv("HANDOFF_MAX_ATTEMPTS", "3")))
    except ValueError:
        return 3


def _retry_minutes() -> float:
    try:
        return max(0.0, float(os.getenv("HANDOFF_RETRY_MINUTES", "5")))
    except ValueError:
        return 5.0


def _investigated(signal_id: str) -> bool:
    """The signal's issue has an investigation attached, i.e. a card went out."""
    item = get_item(signal_id)
    issue = issues.get_issue((item["data"] if item else {}).get("issue_id"))
    return bool(issue and issue["data"].get("investigation_id"))


def _try_handoff(signal_id: str) -> str | None:
    """Run the supervisor for this signal. None on success, else why nobody investigated it."""
    from sre_agent.agents import agent_names, load_agent

    if "supervisor" not in agent_names():
        step(logger, "auto.02.handoff", "supervisor not installed, signal left unrouted",
             level="warning", signal_id=signal_id)
        return "the supervisor agent is not installed"
    step(logger, "auto.02.handoff", "signal -> supervisor", signal_id=signal_id)
    try:
        out = load_agent("supervisor").run(signal_id=signal_id)
        step(logger, "auto.02.handoff", "supervisor finished", signal_id=signal_id, result=str(out)[:200])
    except Exception as e:
        step(logger, "auto.02.handoff", "supervisor failed", level="error", signal_id=signal_id,
             error=f"{type(e).__name__}: {e}"[:300])
        return f"the supervisor crashed: {type(e).__name__}: {e}"[:300]
    if not _investigated(signal_id):  # the supervisor can swallow a sub-agent failure and still return text
        return f"no investigation was produced ({str(out)[:200]})"
    return None


def _handoff_failed(signal_id: str, reason: str) -> None:
    """Remember the failure, schedule a retry, and tell a human (first failure and when giving up)."""
    item = get_item(signal_id)
    data = item["data"] if item else {}
    attempts = int(data.get("handoff_attempts") or 0) + 1
    final = attempts >= _max_attempts()
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=_retry_minutes() * attempts)
    update_item(signal_id, status="handoff_gave_up" if final else "handoff_failed", handoff_failed=True,
                handoff_attempts=attempts, handoff_error=reason, handoff_next=retry_at.isoformat())
    what = f"{data.get('service', '?')} {data.get('error', '?')}"
    if final:
        health.alert(f"handoff:{signal_id}:final", f"Error NOT investigated (gave up): {what}",
                     f"Signal `{signal_id}`: {attempts} attempts failed. Last reason: {reason}. "
                     "A person needs to look at this error and at the agent's log.")
    elif attempts == 1:
        health.alert(f"handoff:{signal_id}:first", f"Error not investigated yet: {what}",
                     f"Signal `{signal_id}`: {reason}. Retrying automatically (up to {_max_attempts()} attempts).")
    step(logger, "auto.02.handoff", "handoff failed", level="warning", signal_id=signal_id, attempts=attempts,
         final=final)


def _throttle(signal_id: str, data: dict) -> None:
    """Over the investigation budget: keep the signal, say so once, and let reconcile release it later."""
    update_item(signal_id, status="throttled", throttled_at=datetime.now(timezone.utc).isoformat())
    health.alert("investigation_budget", "Investigation budget reached",
                 f"{budget.describe()}. New errors are still counted and nothing is lost: the "
                 f"signals wait (latest: {data.get('service', '?')} {data.get('error', '?')}) and are investigated "
                 "automatically when the window frees up. Raise INVESTIGATIONS_PER_HOUR if this is a real incident.",
                 cooldown_minutes=60)
    step(logger, "auto.02.handoff", "over the investigation budget, signal waits", level="warning",
         signal_id=signal_id)


def _on_signal(signal_id: str) -> None:
    """Hand one signal to the supervisor (synchronously); a failed hand-off is retried and reported."""
    bind_trace(signal_id)
    item = get_item(signal_id)
    data = item["data"] if item else {}
    if not budget.allow(data.get("severity")):
        _throttle(signal_id, data)
        return
    budget.record(signal_id)
    reason = _try_handoff(signal_id)
    if reason is not None:
        _handoff_failed(signal_id, reason)
        return
    update_item(signal_id, status="handed_off", handoff_failed=False)


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, default)))
    except ValueError:
        return default


def workers_running() -> bool:
    return any(t.is_alive() for t in _workers)


def enqueue_signal(signal_id: str) -> bool:
    """Queue a signal for investigation (most severe first). False when it is already queued or the queue is full;
    a full queue marks the signal for a later retry, so nothing is dropped."""
    with _queued_lock:
        if signal_id in _queued:
            return False
        if len(_queued) >= _int_env("HANDOFF_QUEUE_MAX", 500):
            update_item(signal_id, status="handoff_failed", handoff_failed=True, handoff_error="investigation queue is full",
                        handoff_next=(datetime.now(timezone.utc) + timedelta(minutes=_retry_minutes() or 5)).isoformat())
            return False
        item = get_item(signal_id)
        rank = _RANK.get(str((item["data"] if item else {}).get("severity", "")).upper(), 2)
        _queued.add(signal_id)
        _queue.put((rank, next(_seq), signal_id))
    return True


def _dispatch(signal_id: str) -> None:
    """Workers running (the server): queue it. Not running (CLI, tests): do it now."""
    if workers_running():
        enqueue_signal(signal_id)
    else:
        _on_signal(signal_id)


def _worker() -> None:
    while not _stop.is_set():
        try:
            _, _, signal_id = _queue.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            _on_signal(signal_id)
        except Exception as e:  # noqa: BLE001 - one bad signal must not kill the worker
            step(logger, "auto.02.handoff", "worker crashed on a signal", level="error", signal_id=signal_id,
                 error=f"{type(e).__name__}: {e}"[:300])
        finally:
            with _queued_lock:
                _queued.discard(signal_id)
            _queue.task_done()


def start_workers(count: int | None = None) -> None:
    if workers_running():
        return
    _stop.clear()
    _workers[:] = [threading.Thread(target=_worker, daemon=True, name=f"sre-auto-handoff-{i}")
                   for i in range(count or _int_env("HANDOFF_WORKERS", 2))]
    for t in _workers:
        t.start()


def stop_workers(timeout: float = 5.0) -> None:
    _stop.set()
    for t in _workers:
        t.join(timeout)
    _workers.clear()
    with _queued_lock:
        _queued.clear()
    while not _queue.empty():
        try:
            _queue.get_nowait()
        except queue.Empty:
            break


def reconcile_signals(now: datetime | None = None, min_age_minutes: float | None = None) -> list[str]:
    """Hand off signals that were stored but never investigated.

    A restart or crash between "signal stored" and "hand-off done", a full queue, or a paused budget all leave a
    signal with no card. This finds them (open issue, no investigation) and queues the newest one per issue.
    Signals that need nothing (counted repeats, issues that already have a card) are marked so they are not
    looked at again."""
    now = now or datetime.now(timezone.utc)
    min_age = timedelta(minutes=min_age_minutes if min_age_minutes is not None
                        else float(os.getenv("RECONCILE_MIN_AGE_MINUTES", "2") or 2))
    since = now - timedelta(hours=float(os.getenv("RECONCILE_LOOKBACK_HOURS", "24") or 24))
    candidates = (list_items(kind="signal", status="new", since=since)
                  + list_items(kind="signal", status="throttled", since=since))
    out, seen = [], set()
    for item in candidates:  # newest first within each status
        created = issues._parse(item["created_at"])
        if created is None or now - created < min_age:
            continue
        d, sid = item["data"], item["id"]
        issue_id = d.get("issue_id")
        if not issue_id:  # crashed between storing the signal and linking it to its issue
            issue_id, action = issues.record_occurrence(d, sid)
            update_item(sid, issue_id=issue_id, **({"status": "counted"} if action == "count" else {}))
            if action == "count":
                continue
        issue = issues.get_issue(issue_id)
        state = (issue["data"] if issue else {}).get("state")
        if issue is None or state != "open":
            update_item(sid, status="counted")
            continue
        if issue["data"].get("investigation_id"):
            update_item(sid, status="handed_off")
            continue
        if issue_id in seen:
            continue
        seen.add(issue_id)
        if item["status"] == "throttled" and not budget.allow(d.get("severity")):
            continue
        if sid in _queued:
            continue
        step(logger, "auto.10.reconcile", "signal was never investigated, handing it off again", level="warning",
             signal_id=sid, issue_id=issue_id)
        out.append(sid)
        update_item(sid, status="new")
        _dispatch(sid)
    return out


def retry_handoffs(now: datetime | None = None) -> list[str]:
    """Re-run the hand-off for signals whose last attempt failed and whose retry time has come."""
    now = now or datetime.now(timezone.utc)
    retried = []
    for item in list_items(kind="signal", status="handoff_failed"):
        due = issues._parse(item["data"].get("handoff_next"))
        if due is None or due <= now:
            retried.append(item["id"])
            update_item(item["id"], status="new")
            _dispatch(item["id"])
    return retried


def release_held() -> list[str]:
    """Quiet hours are over: send the cards that were held. One per issue (the newest signal)."""
    if severity_rules.in_quiet_hours():
        return []
    released, seen = [], set()
    for item in list_items(kind="signal", status="held"):  # newest first
        issue_id = item["data"].get("issue_id")
        update_item(item["id"], status="new", held=False)
        if issue_id in seen:
            continue
        seen.add(issue_id)
        released.append(item["id"])
    for signal_id in released:
        step(logger, "auto.07.held", "quiet hours over, releasing held signal", signal_id=signal_id)
        _dispatch(signal_id)
    return released


def _watch_loop(source: str) -> None:
    from sre_agent.agents import agent_names, load_agent

    if "monitor" not in agent_names():
        step(logger, "auto.01.start", "monitor not installed, auto mode idle", level="warning")
        return
    while True:  # restart the watcher if it dies (missing log file, SSH drop, ...)
        try:
            load_agent("monitor").run(source=source, mode="watch", on_signal=_dispatch)  # never blocks the poll loop
        except Exception as e:
            step(logger, "auto.01.start", "watcher crashed, restarting", level="error",
                 error=f"{type(e).__name__}: {e}"[:300], retry_in_s=settings.poll_seconds)
            from sre_agent.sre.monitor.sources import hint_for

            health.report(f"watcher:{source}", False, f"Log watcher failing: {source or 'default source'}",
                          (f"The monitor cannot read this log, so NEW ERRORS ARE NOT BEING SEEN. "
                           f"{type(e).__name__}: {e}" + hint_for(source, e))[:700], fails_needed=1, page=True)
        time.sleep(max(1, settings.poll_seconds))


NL = chr(10)
SWEEP_SECONDS = int(os.getenv("ISSUE_SWEEP_SECONDS", "60"))


def send_reminder(issue: dict) -> None:
    """Email a still-failing developer-fix / fix-pending issue: what it is, how often, what was found."""
    inv = get_item(issue["investigation_id"]) if issue.get("investigation_id") else None
    found = (inv["data"].get("summary") or inv["data"].get("rca") or "") if inv else ""
    who = "a developer chose to fix it themselves" if issue["state"] == "developer_fix" else "the fix was approved"
    minutes = issues.quiet_window().total_seconds() / 60
    body = NL.join([
        f"Reminder: {issue.get('service')} · {issue.get('error')} is still failing and not marked fixed.",
        f"State: {issue['state'].replace('_', ' ')} ({who}).",
        f"Occurrences: {issues.describe(issue)}.",
        "" if not found else f"What the analysis found: {str(found)[:600]}",
        f"The issue closes by itself once the error has not appeared for {minutes:g} minutes.",
    ])
    notify(f"reminder: {issue.get('service')} {issue.get('error')} still failing", body)
    step(logger, "auto.04.reminder", "reminder sent", issue_id=issue["id"], state=issue["state"],
         count=issue.get("count"))


def _reminder_buttons(investigation_id: str | None) -> list[tuple]:
    """Freshly signed decision buttons for a card that is still waiting (its first links may have expired)."""
    from sre_agent.core.approvals import fresh_buttons
    from sre_agent.sre.incident.publish import STAGE

    inv = get_item(investigation_id) if investigation_id else None
    if not inv or inv["status"] != f"awaiting_{STAGE}":
        return []
    return fresh_buttons(investigation_id, STAGE, hide=("approve", "reject") if inv["data"].get("notice") else ())


def send_unanswered(issue: dict) -> None:
    """The card for this issue has had no answer: send it again, and escalate to a second person when due."""
    inv = get_item(issue["investigation_id"]) if issue.get("investigation_id") else None
    found = (inv["data"].get("summary") or inv["data"].get("rca") or "") if inv else ""
    title = f"reminder: {issue.get('service')} {issue.get('error')} is waiting for a decision"
    body = NL.join([
        f"Nobody has answered the card for {issue.get('service')} · {issue.get('error')} "
        f"({issue.get('waiting_hours', 0):.1f} h) and it is still happening.",
        f"Occurrences: {issues.describe(issue)}.",
        "" if not found else f"What the analysis found: {str(found)[:600]}",
        "Choose a button below (the links are new; the ones on the original card may have expired).",
    ])
    notify(title, body, buttons=_reminder_buttons(issue.get("investigation_id")) or None)
    if issue.get("escalate"):
        paging.escalate(f"ESCALATION: {title}", body)
    step(logger, "auto.04.reminder", "unanswered-card reminder sent", issue_id=issue["id"],
         escalated=bool(issue.get("escalate")))


def send_verified(issue: dict) -> None:
    """An issue closed because it stayed quiet: say so, and say what that quiet follows."""
    was = {"fix_pending": "after the fix was approved", "developer_fix": "after a developer took it",
           "open": "on its own, with no action taken"}.get(issue.get("was", ""), "")
    minutes = issues.quiet_window().total_seconds() / 60
    notify(f"recovered: {issue.get('service')} {issue.get('severity', 'HIGH')} {issue.get('error')}",
           NL.join([f"No occurrence for {minutes:g} min, so this issue is closed ({was}).",
                    f"Occurrences in total: {issues.describe(issue)}.",
                    "If it comes back, you will get a new card and it will be marked as a failed fix."]))
    step(logger, "auto.08.verified", "issue verified quiet", issue_id=issue["id"], was=issue.get("was"))


def _guard(name: str, fn) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - one failing job must not stop the others
        step(logger, "auto.06.jobs", f"{name} failed", level="error", error=f"{type(e).__name__}: {e}"[:300])


def _sweep_issues() -> None:
    res = issues.sweep_all()
    for due in res["reminders"]:
        send_reminder(due)
    for due in res["unanswered"]:
        send_unanswered(due)
    for done in res["verified"]:
        send_verified(done)


_last_run: dict[str, float] = {}


def _every(name: str, seconds: float, immediate: bool = True) -> bool:
    """True at most once per `seconds` for this job (the housekeeping loop runs much more often).
    immediate=False: the first call only starts the clock (nothing is due the moment the server starts)."""
    now = time.monotonic()
    if name not in _last_run:
        _last_run[name] = now
        return immediate
    if now - _last_run[name] < seconds:
        return False
    _last_run[name] = now
    return True


def send_low_digest() -> bool:
    """One message for everything that was kept quiet: low-priority issues and lines the ignore list dropped."""
    lows, ignored = issues.low_digest(), ignorelist.digest_rows()
    if not lows and not ignored:
        return False
    parts = []
    if lows:
        parts += ["*LOW PRIORITY*"] + [f"• {i.get('service')} · {i.get('error')}: {i['new']} new, {i.get('count', 0)} in total"
                                       for i in lows[:10]]
        parts += [f"…and {len(lows) - 10} more"] if len(lows) > 10 else []
    if ignored:
        parts += ["", "*IGNORED BY YOUR RULES*"] + [f"• `{r['pattern']}`: {r['new']} line(s)" for r in ignored[:10]]
    parts += ["", f"A low-priority error becomes a normal card after {issues.low_escalate_count()} repeats."]
    notify(f"digest: {len(lows)} low-priority issue(s), {sum(r['new'] for r in ignored)} ignored line(s)", NL.join(parts))
    step(logger, "auto.11.digest", "digest sent", low=len(lows), ignored=len(ignored))
    return True


def _low_digest_periodic() -> None:
    hours = float(os.getenv("LOW_DIGEST_HOURS", "24") or 0)
    if hours > 0 and _every("low-digest", hours * 3600, immediate=False):
        send_low_digest()


def _reconcile_periodic() -> None:
    if _every("reconcile", float(os.getenv("RECONCILE_SECONDS", "300") or 300)):
        reconcile_signals()


def _check_backlog() -> None:
    if not workers_running():
        return
    depth, limit = _queue.qsize(), _int_env("HANDOFF_BACKLOG_ALERT", 50)
    health.report("handoff_backlog", depth < limit, "Error investigation is backed up",
                  f"{depth} signals are waiting for an investigation worker ({len(_workers)} running). New errors are "
                  "stored and counted, but their cards are delayed. Check the model provider and "
                  "HANDOFF_WORKERS.", fails_needed=2)


def _retention_daily() -> None:
    if _every("retention", 86400):
        retention.run()


def _poll_prs() -> None:
    if _every("pr-poll", float(os.getenv("PR_POLL_SECONDS", "300") or 300)):
        from sre_agent.sre import ship

        ship.poll_prs()


def _clean_worktrees() -> None:
    if _every("janitor", 3600):
        from sre_agent.core import git_tools
        from sre_agent.sre import ship

        removed = git_tools.prune_stale(float(os.getenv("CODING_WORKTREE_TTL_DAYS", "7") or 7), ship.pending_worktrees())
        if removed:
            step(logger, "auto.09.janitor", "stale work areas removed", removed=removed)


def _issue_loop() -> None:
    """Housekeeping, forever: close quiet issues, remind, report missed jobs, retry failed hand-offs."""
    while True:
        _guard("issue sweep", _sweep_issues)
        _guard("scheduled job check", lambda: [_dispatch(s) for s in schedules.check_missed()])
        _guard("handoff retry", retry_handoffs)
        _guard("held release", release_held)
        _guard("signal reconcile", _reconcile_periodic)
        _guard("low digest", _low_digest_periodic)
        _guard("queue health", _check_backlog)
        _guard("retention", _retention_daily)
        _guard("pull request poll", _poll_prs)
        _guard("worktree cleanup", _clean_worktrees)
        time.sleep(max(5, SWEEP_SECONDS))


def start(source: str = "") -> bool:
    """Start the background watcher once (no-op unless SRE_AUTO_RUN=true)."""
    global _started
    if _started or not enabled():
        return False
    _started = True
    source = source or os.getenv("MONITOR_LOG_PATH", "") or os.getenv("MONITOR_SOURCE", "")
    step(logger, "auto.01.start", "auto mode ON: watcher thread starting", source=source,
         poll_s=settings.poll_seconds)
    start_workers()  # model work happens here, so a slow investigation never stops the watchers reading logs
    for i, one in enumerate([s.strip() for s in source.split(",") if s.strip()] or [""]):  # one watcher per source
        threading.Thread(target=_watch_loop, args=(one,), daemon=True, name=f"sre-auto-monitor-{i}").start()
    threading.Thread(target=_issue_loop, daemon=True, name="sre-auto-issues").start()
    threading.Thread(target=health.loop, daemon=True, name="sre-auto-health").start()
    if revision.reply_enabled():
        from sre_agent.sre import email_reply

        threading.Thread(target=email_reply.loop, daemon=True, name="sre-auto-email-replies").start()
        step(logger, "auto.05.email", "reading the mailbox for plan replies", host=os.getenv("IMAP_HOST", "imap.gmail.com"))
    return True


def after_approval(investigation_id: str, attempt: int = 1) -> None:
    """Human approved the fix (or asked for a retry) -> run the coding agent in the background (auto mode only).

    A retry uses a fresh incident id (<investigation>-r<n>), so it gets a fresh branch and work area. A run that
    is blocked, fails or crashes goes to fixfail (card with Retry / I'll fix this / Ignore); it is never silent."""
    if not enabled():
        return
    coding_id = investigation_id if attempt <= 1 else f"{investigation_id}-r{attempt - 1}"

    def _run() -> None:
        from sre_agent.agents import agent_names, load_agent
        from sre_agent.core.db import get_item

        if "coding" not in agent_names():
            step(logger, "auto.03.coding", "coding agent not installed", level="warning",
                 investigation_id=investigation_id)
            fixfail.report_failure(investigation_id, error="the coding agent is not installed")
            return
        inv = get_item(investigation_id)
        signal_id = (inv["data"] if inv else {}).get("signal_id")
        step(logger, "auto.03.coding", "approval -> coding agent", investigation_id=investigation_id,
             signal_id=signal_id)
        try:
            out = load_agent("coding").run(incident_id=coding_id, signal=signal_id, investigation=investigation_id)
            step(logger, "auto.03.coding", "coding finished", investigation_id=investigation_id, attempt=attempt,
                 status=out.get("status") if isinstance(out, dict) else str(out)[:100])
        except Exception as e:
            step(logger, "auto.03.coding", "coding crashed", level="error",
                 investigation_id=investigation_id, error=f"{type(e).__name__}: {e}"[:300])
            fixfail.report_failure(investigation_id, error=f"the coding agent crashed: {type(e).__name__}: {e}"[:300])
            return
        if not (isinstance(out, dict) and out.get("status") == "completed"):
            fixfail.report_failure(investigation_id, out if isinstance(out, dict) else None, error=str(out)[:300])
            return
        try:
            from sre_agent.sre import ship

            fix_id = ship.offer(out, investigation_id)  # second approval: commit, push, pull request
            step(logger, "auto.04.ship", "fix-ready card sent" if fix_id else "no fix-ready card",
                 investigation_id=investigation_id, fix_id=fix_id)
        except Exception as e:  # the fix exists in its work area; only the hand-over failed
            step(logger, "auto.04.ship", "shipping crashed", level="error", investigation_id=investigation_id,
                 error=f"{type(e).__name__}: {e}"[:300])
            health.alert(f"ship:{investigation_id}", "A finished fix could not be handed over",
                         f"The coding agent finished a fix for investigation `{investigation_id}` but preparing "
                         f"it for review failed: {type(e).__name__}: {e}"[:400])

    def _guarded() -> None:
        try:
            _run()
        except Exception as e:  # noqa: BLE001 - a background thread must never die without a trace
            step(logger, "auto.03.coding", "coding thread failed", level="error",
                 investigation_id=investigation_id, error=f"{type(e).__name__}: {e}"[:300])

    threading.Thread(target=_guarded, daemon=True, name="sre-auto-coding").start()

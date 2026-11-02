"""Auto mode: SRE_AUTO_RUN=true makes the server run the whole loop by itself.

    monitor (watch thread) --signal--> supervisor --> incident (card)
    human clicks Approve --> coding (background thread)

Each agent is still called through its public run() only (load_agent), so
deleting an agent folder just shrinks the chain. Off by default.
"""
import os
import threading
import time

from sre_agent.core.config import settings
from sre_agent.core.db import get_item
from sre_agent.core.notify import notify
from sre_agent.core.sre_log import bind_trace, get_logger, step
from sre_agent.sre import issues, revision

logger = get_logger("auto")
_started = False


def enabled() -> bool:
    return os.getenv("SRE_AUTO_RUN", "false").strip().lower() == "true"


def _on_signal(signal_id: str) -> None:
    """Monitor filed a signal -> hand it to the supervisor."""
    from sre_agent.agents import agent_names, load_agent

    bind_trace(signal_id)
    if "supervisor" not in agent_names():
        step(logger, "auto.02.handoff", "supervisor not installed, signal left unrouted",
             level="warning", signal_id=signal_id)
        return
    step(logger, "auto.02.handoff", "signal -> supervisor", signal_id=signal_id)
    try:
        out = load_agent("supervisor").run(signal_id=signal_id)
        step(logger, "auto.02.handoff", "supervisor finished", signal_id=signal_id, result=str(out)[:200])
    except Exception as e:
        step(logger, "auto.02.handoff", "supervisor failed", level="error", signal_id=signal_id,
             error=f"{type(e).__name__}: {e}"[:300])


def _watch_loop(source: str) -> None:
    from sre_agent.agents import agent_names, load_agent

    if "monitor" not in agent_names():
        step(logger, "auto.01.start", "monitor not installed, auto mode idle", level="warning")
        return
    while True:  # restart the watcher if it dies (missing log file, SSH drop, ...)
        try:
            load_agent("monitor").run(source=source, mode="watch", on_signal=_on_signal)
        except Exception as e:
            step(logger, "auto.01.start", "watcher crashed, restarting", level="error",
                 error=f"{type(e).__name__}: {e}"[:300], retry_in_s=settings.poll_seconds)
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


def _issue_loop() -> None:
    """Close quiet issues and send due reminders, forever."""
    while True:
        try:
            for due in issues.sweep():
                send_reminder(due)
        except Exception as e:
            step(logger, "auto.04.reminder", "issue sweep failed", level="error",
                 error=f"{type(e).__name__}: {e}"[:300])
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
    threading.Thread(target=_watch_loop, args=(source,), daemon=True, name="sre-auto-monitor").start()
    threading.Thread(target=_issue_loop, daemon=True, name="sre-auto-issues").start()
    if revision.reply_enabled():
        from sre_agent.sre import email_reply

        threading.Thread(target=email_reply.loop, daemon=True, name="sre-auto-email-replies").start()
        step(logger, "auto.05.email", "reading the mailbox for plan replies", host=os.getenv("IMAP_HOST", "imap.gmail.com"))
    return True


def after_approval(investigation_id: str) -> None:
    """Human approved the fix -> run the coding agent in the background (auto mode only)."""
    if not enabled():
        return

    def _run() -> None:
        from sre_agent.agents import agent_names, load_agent
        from sre_agent.core.db import get_item

        if "coding" not in agent_names():
            step(logger, "auto.03.coding", "coding agent not installed", level="warning",
                 investigation_id=investigation_id)
            return
        inv = get_item(investigation_id)
        signal_id = (inv["data"] if inv else {}).get("signal_id")
        step(logger, "auto.03.coding", "approval -> coding agent", investigation_id=investigation_id,
             signal_id=signal_id)
        try:
            out = load_agent("coding").run(incident_id=investigation_id, signal=signal_id,
                                           investigation=investigation_id)
            step(logger, "auto.03.coding", "coding finished", investigation_id=investigation_id,
                 status=out.get("status") if isinstance(out, dict) else str(out)[:100])
            if isinstance(out, dict) and out.get("status") == "completed":
                from sre_agent.sre import ship

                fix_id = ship.offer(out, investigation_id)  # second approval: commit, push, pull request
                step(logger, "auto.04.ship", "fix-ready card sent" if fix_id else "no fix-ready card",
                     investigation_id=investigation_id, fix_id=fix_id)
        except Exception as e:
            step(logger, "auto.03.coding", "coding crashed", level="error",
                 investigation_id=investigation_id, error=f"{type(e).__name__}: {e}"[:300])

    threading.Thread(target=_run, daemon=True, name="sre-auto-coding").start()

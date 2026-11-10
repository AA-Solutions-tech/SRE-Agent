"""A coding run that produced no usable fix must not be a dead end.

When the coding agent is blocked, fails, or crashes, the issue used to sit in `fix_pending` ("the fix was
approved") with one chat message and no way forward. Now:

  * the issue goes back to `open` with the reason on record, so unanswered-card reminders and escalation apply;
  * a "fix failed" card says what happened and why, and offers  [Retry the fix]  [I'll fix this]  [Ignore];
  * a retry runs the coding agent again on a FRESH branch (the failed attempt's work area was already removed),
    up to CODING_MAX_RETRIES times;
  * a run that was blocked before starting (missing fields, low confidence, a path the reviewer ruled out) is not
    retryable: asking again gives the same answer, so the card says what needs to change instead.
"""
import os

from sre_agent.core.approvals import on_approval, request_approval
from sre_agent.core.db import create_item, get_item, update_item
from sre_agent.core.sre_log import bind_trace, get_logger, step
from sre_agent.sre import issues

STAGE = "fix_retry"
logger = get_logger("fixfail")
NL = chr(10)


def max_retries() -> int:
    try:
        return max(0, int(os.getenv("CODING_MAX_RETRIES", "2")))
    except ValueError:
        return 2


def _issue_id(inv_data: dict) -> str | None:
    sig = get_item(inv_data["signal_id"]) if inv_data.get("signal_id") else None
    return (sig["data"] if sig else {}).get("issue_id")


def report_failure(investigation_id: str, result: dict | None = None, error: str = "") -> str | None:
    """Reset the issue and send the "fix failed" card. Returns the card's item id (None if the record is gone)."""
    inv = get_item(investigation_id)
    if inv is None:
        return None
    d = inv["data"]
    bind_trace(d.get("signal_id"))
    result = result or {}
    blocked = result.get("status") == "blocked"
    reason = str(result.get("failure_reason") or error or "unknown reason")[:400]
    needed = [str(n) for n in (result.get("clarification_needed") or []) if str(n) != reason][:5]
    attempts = int(d.get("coding_attempts") or 0) + 1
    update_item(investigation_id, coding_attempts=attempts, last_coding_failure=reason, fix_queued=False)
    issue_id = _issue_id(d)
    issues.mark_fix_failed(issue_id, reason)
    retryable = (not blocked) and attempts <= max_retries()
    fix_id = create_item("fix_failed", {"investigation_id": investigation_id, "signal_id": d.get("signal_id"),
                                        "issue_id": issue_id, "attempts": attempts, "reason": reason,
                                        "retryable": retryable})
    service, err = d.get("service", "?"), d.get("error", "?")
    why_no_retry = ("the plan itself was rejected before any code was touched, so asking again gives the same "
                    "answer: use Request changes on the plan card, or fix it yourself" if blocked else
                    f"the retry limit ({max_retries()}) was reached")
    body = NL.join([
        "*INCIDENT*", f"{service} · {err} · attempt {attempts}", "",
        "*WHAT HAPPENED*",
        ("The coding agent was blocked before it started." if blocked else
         "The coding agent ran but could not produce a fix that passes its checks.")
        + " Nothing was committed or pushed, and its work area was removed.", "",
        "*WHY*", reason, *(f"• {n}" for n in needed), "",
        "*ON APPROVE*",
        (f"• Retry the fix: runs the coding agent again on a fresh branch (retry {attempts} of {max_retries()})."
         if retryable else f"• Retry is not offered: {why_no_retry}."),
        "• I'll fix this: you take it; you are reminded until the error stops.",
        "• Ignore: repeats are counted silently.",
    ])
    request_approval(fix_id, STAGE, f"fix failed: {service} {d.get('severity', 'HIGH')} {err} [trace {d.get('signal_id')}]",
                     body, hide=("reject",) if retryable else ("approve", "reject"),
                     labels={"approve": "Retry the fix"})
    step(logger, "fixfail.01.card", "fix failed, card sent", investigation_id=investigation_id, attempts=attempts,
         retryable=retryable, blocked=blocked, level="warning")
    return fix_id


@on_approval(STAGE, extra_actions={"developer": "I'll fix this", "ignore": "Ignore"})
def handle_fix_retry(item_id: str, action: str) -> str:
    item = get_item(item_id)
    d = item["data"] if item else {}
    bind_trace(d.get("signal_id"))
    issue_id = d.get("issue_id")
    if action == "developer":
        issues.mark_developer_fix(issue_id)
        return "recorded: a developer will fix this; you get reminders until the error stops"
    if action == "ignore":
        issues.mark_ignored(issue_id)
        return "ignored: repeats are counted silently"
    if action != "approve":
        issues.mark_rejected(issue_id)
        return "left as it is"
    if not d.get("retryable"):
        return "retry is not available for this failure"
    from sre_agent.sre import auto

    if not auto.enabled():
        return "retry recorded, but SRE_AUTO_RUN is off so the coding agent was not started; run it by hand"
    issues.mark_fix_pending(issue_id)
    auto.after_approval(d["investigation_id"], attempt=int(d.get("attempts", 1)) + 1)
    return f"retrying the fix (attempt {int(d.get('attempts', 1)) + 1})"

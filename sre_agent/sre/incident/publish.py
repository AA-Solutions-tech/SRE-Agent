"""Shared by the incident agent and the revision service: related signals, and publishing a plan card.

A plan is an `investigation` record. Publishing one builds the card (fixed layout when the analysis
has a root cause, plain fallback otherwise), sends it with the signed links, and adds the
"Request changes" form link when revisions are enabled and there is an analysis to revise.
"""
from datetime import datetime, timedelta, timezone

from sre_agent.core.approvals import request_approval
from sre_agent.core.db import list_items
from sre_agent.core.notify import approval_token, feedback_url
from sre_agent.core.sre_log import get_logger, step
from sre_agent.sre import issues

from .diagnose import build_notice_card
from .rca import build_analysis_card, build_card

STAGE = "incident_fix"  # approval gate name; namespaced so it never collides (kit template uses "act")
logger = get_logger("incident")


def related_signals(signal: dict, signal_id: str) -> list[dict]:
    """Other signals of the same bug (same fingerprint), else of the same service."""
    fp, service = signal.get("fingerprint"), signal.get("service")
    return [
        {"id": i["id"], "error": i["data"].get("error"), "ts": i["data"].get("ts")}
        for i in list_items(kind="signal", since=datetime.now(timezone.utc) - timedelta(days=7), limit=2000)
        if isinstance(i["data"], dict)  # skip malformed records (e.g. double-encoded)
        and i["id"] != signal_id
        and ((i["data"].get("fingerprint") == fp) if fp else i["data"].get("service") == service)
    ]


def publish_card(inv_id: str, signal: dict, signal_id: str, analysis: dict, rca: str, occurrences: int,
                 issue: dict | None, version: int = 1, changes: list[str] | None = None,
                 instruction: str = "", ai_error: str | None = None):
    """Send the approval card for investigation `inv_id` (one call per plan version)."""
    from sre_agent.sre import revision  # lazy: revision imports this module

    if analysis.get("notice"):  # operational error: tell the owner what to do, no code-fix approval
        title, body = build_notice_card(signal, analysis, occurrences, signal_id, issue=issue)
        deliveries = request_approval(inv_id, STAGE, f"{title} [trace {signal_id}]", body, hide=("approve", "reject"))
        step(logger, "incident.04.card", "notice card sent (no code-fix approval)", investigation_id=inv_id,
             category=analysis.get("category"))
        return deliveries
    if analysis.get("root_cause"):
        title, body = build_analysis_card(signal, analysis, occurrences, signal_id, issue=issue, version=version,
                                          changes=changes, instruction=instruction,
                                          revisions_left=revision.revisions_left(version))
    else:
        title, body = build_card(signal, rca, occurrences, ai_error=ai_error)
    title = f"{title} [trace {signal_id}]"
    links, headers = [], {}
    if revision.enabled() and analysis.get("root_cause"):
        token = approval_token(inv_id, f"feedback-v{version}")
        # 4th element: the Slack app turns this into a modal button (plain URL button when no app is configured)
        links.append(("Request changes", feedback_url(inv_id, version), "", f"request_changes|{inv_id}|{version}|{token}"))
        if revision.reply_enabled():
            from sre_agent.sre import email_reply

            headers = email_reply.headers_for(inv_id, version)
    deliveries = request_approval(inv_id, STAGE, title, body, extra_links=links, email_headers=headers or None)
    step(logger, "incident.04.card", "approval card sent", investigation_id=inv_id, version=version,
         request_changes_link=bool(links))
    return deliveries

"""SRE product endpoints (APIRouter, included by server.py).

No business logic here: these functions call agents and read records,
never implement. Removing an agent folder shrinks what they can route to.
"""
from fastapi import APIRouter, HTTPException, Query

from sre_agent.agents import agent_names, load_agent
from sre_agent.core.db import create_item, get_item, list_items
from sre_agent.core.sre_log import get_logger

logger = get_logger("server")

router = APIRouter()


@router.get("/agents")
def agents_list():
    """Every installed agent with a one-line description (removing a folder removes it here too)."""
    out = []
    for name in agent_names():
        doc = (load_agent(name).__doc__ or "").strip().splitlines()
        out.append({"name": name, "description": doc[0] if doc else ""})
    return {"agents": out}


@router.get("/signals")
def signals_list(
    limit: int = Query(20, ge=1, le=100),
    service: str | None = None,
    severity: str | None = None,
):
    """Newest-first signal records, optionally filtered (e.g. /signals?severity=HIGH)."""
    items = list_items(kind="signal")
    if service:
        items = [i for i in items if i["data"].get("service") == service]
    if severity:
        items = [i for i in items if i["data"].get("severity") == severity]
    return {"signals": items[:limit]}


@router.get("/signals/{signal_id}")
def signal_get(signal_id: str):
    item = get_item(signal_id)
    if item is None or item["kind"] != "signal":
        raise HTTPException(404, "unknown signal")
    return item


@router.post("/signals")
def signal_inject(event: dict, supervise: bool = Query(False)):
    """Store an injected event as a signal; ?supervise=true runs the supervisor on it immediately."""
    signal_id = create_item("signal", event)
    logger.info("http.signal_inject", extra={"signal_id": signal_id, "supervise": supervise,
                                             "service": event.get("service"),
                                             "severity": event.get("severity")})
    if not supervise:
        return {"signal_id": signal_id}
    if "supervisor" not in agent_names():
        raise HTTPException(409, "supervisor agent not installed")
    return {"signal_id": signal_id, "result": load_agent("supervisor").run(signal_id=signal_id)}


ISSUE_FIELDS = ("service", "error", "severity", "state", "count", "first_seen", "last_seen", "state_since",
                "regressions", "revisions", "investigation_id", "fingerprint", "branch", "commit", "pr_url",
                "last_delivery")


def _issue_view(item: dict) -> dict:
    return {"id": item["id"], **{k: item["data"].get(k) for k in ISSUE_FIELDS}}


@router.get("/stats")
def stats(days: int = Query(7, ge=1, le=90)):
    """What a dashboard needs to tell "no data" from "on track". Decisions, analysis quality and delivery over the
    last `days`. With zero decisions `decision_metrics` says "no data": nobody has answered a card yet."""
    from datetime import datetime, timedelta, timezone

    from sre_agent.core.db import count_items

    since = datetime.now(timezone.utc) - timedelta(days=days)
    investigations = list_items(kind="investigation", since=since, limit=5000)
    decided = [i for i in investigations if i["data"].get("decided_from") or i["data"].get("decided_by")]
    via_slack = [i for i in decided if (i["data"].get("decided_by") or {}).get("via") == "slack"]
    fallback = [i for i in investigations if i["data"].get("ai_error")]
    issues_seen = list_items(kind="issue", since=since, limit=5000)
    delivered = [i for i in issues_seen if any(d.get("ok") for d in i["data"].get("last_delivery") or [])]
    failed = [i for i in issues_seen if i["data"].get("last_delivery")
              and not any(d.get("ok") is not False for d in i["data"]["last_delivery"])]
    return {
        "days": days,
        "signals": count_items("signal", since=since),
        "issues": len(issues_seen),
        "investigations": len(investigations),
        "ai_analysed": len(investigations) - len(fallback),
        "ai_template_fallback": len(fallback),
        "decisions": len(decided),
        "decisions_via_slack": len(via_slack),
        "cards_delivered": len(delivered),
        "cards_delivery_failed": len(failed),
        "decision_metrics": "available" if decided else "no data",
    }


@router.get("/issues")
def issues_list(state: str | None = None, service: str | None = None, limit: int = Query(20, ge=1, le=100)):
    """Read-only list of tracked issues, newest first. ?state=open|fix_pending|developer_fix|ignored|rejected|fixed"""
    items = list_items(kind="issue")
    if state:
        items = [i for i in items if i["data"].get("state") == state]
    if service:
        items = [i for i in items if i["data"].get("service") == service]
    return {"issues": [_issue_view(i) for i in items[:limit]]}


@router.get("/issues/{issue_id}")
def issue_get(issue_id: str):
    item = get_item(issue_id)
    if item is None or item["kind"] != "issue":
        raise HTTPException(404, "unknown issue")
    return {**_issue_view(item), "signal_ids": item["data"].get("signal_ids", [])}

"""Investigate agent (folder `incident`, displays as "investigate").

Consumes the monitor's signal JSON record, produces an RCA, posts a human
summary card to Slack with an Approve/Reject gate. Approving queues the
fix for the coding agent; this agent never changes code itself.

    python run.py incident --signal-id <id>
    POST /run/incident  {"signal_id": "..."}
"""
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from sre_agent.core.approvals import on_approval
from sre_agent.core.db import create_item, get_item, list_items, update_item
from sre_agent.core.llm import get_llm, text_of
from sre_agent.core.sre_log import bind_trace, get_logger, step
from sre_agent.sre import issues

logger = get_logger("incident")

from .publish import STAGE, publish_card, related_signals  # STAGE = approval gate name
from .rca import build_rca_prompt, heuristic_rca


class State(TypedDict, total=False):
    signal_id: str
    signal: dict
    related: list
    analysis: dict
    rca: str
    investigation_id: str
    summary: str


def load(state: State) -> State:
    bind_trace(state["signal_id"])
    item = get_item(state["signal_id"])
    if item is None or item["kind"] != "signal":
        step(logger, "incident.01.load", "unknown signal", level="error", signal_id=state["signal_id"])
        raise ValueError(f"unknown signal: {state['signal_id']}")
    data = dict(item["data"])
    step(logger, "incident.01.load", "signal loaded", signal_id=state["signal_id"],
         service=data.get("service"), severity=data.get("severity"), error=data.get("error"))
    return {"signal": data}


def gather(state: State) -> State:
    bind_trace(state["signal_id"])
    service = state["signal"].get("service")
    related = related_signals(state["signal"], state["signal_id"])
    step(logger, "incident.02.gather", "related signals found", service=service, related=len(related))
    return {"related": related}


def inspect_code(state: State) -> State:
    bind_trace(state["signal_id"])
    """Search REPO_DIR for the failing code; fields feed the coding agent. Never fails the run."""
    from .code_inspect import inspect

    try:
        return {"analysis": inspect(state["signal"], state["related"]) or {}}
    except Exception as e:
        step(logger, "incident.02b.inspect", "code inspection crashed, continuing without it",
             level="error", error=f"{type(e).__name__}: {e}"[:300])
        return {"analysis": {}}


def rca(state: State) -> State:
    bind_trace(state["signal_id"])
    occurrences = len(state["related"]) + 1
    analysis = state.get("analysis") or {}
    if analysis.get("root_cause"):
        text = (f"Likely cause: {analysis['root_cause']}\n"
                f"Files: {', '.join(analysis['affected_files'])} (confidence {analysis['confidence']:.2f})\n"
                f"Suggested fix: {analysis['remediation']}")
        step(logger, "incident.03.rca", "RCA from code analysis", chars=len(text), occurrences=occurrences)
        return {"rca": text}
    try:
        llm = get_llm(temperature=0.0)
        out = text_of(llm.invoke(build_rca_prompt(state["signal"], state["related"]))).strip()
        if not out:
            raise ValueError("empty LLM reply")
        step(logger, "incident.03.rca", "RCA from LLM", chars=len(out), occurrences=occurrences)
        return {"rca": out}
    except Exception as e:
        step(logger, "incident.03.rca", "LLM failed, using heuristic RCA", level="warning",
             error=f"{type(e).__name__}: {e}"[:200], occurrences=occurrences)
        return {"rca": heuristic_rca(state["signal"], occurrences)}


def card(state: State) -> State:
    bind_trace(state["signal_id"])
    signal, occurrences = state["signal"], len(state["related"]) + 1
    issue = issues.get_issue(signal.get("issue_id"))
    if issue:
        occurrences = issue["data"].get("count", occurrences)
    analysis = state.get("analysis") or {}
    inv_id = create_item("investigation", {
        "signal_id": state["signal_id"],
        "service": signal.get("service"),
        "severity": signal.get("severity"),
        "error": signal.get("error"),
        "rca": state["rca"],
        "version": 1,
        **analysis,  # root_cause, remediation, affected_files, ... for coding
    })
    issues.attach_investigation(signal.get("issue_id"), inv_id)
    # Single card with buttons (request_approval notifies); no separate notify.
    publish_card(inv_id, signal, state["signal_id"], analysis, state["rca"], occurrences, issue)
    step(logger, "incident.04.card", "investigation filed, approval card sent",
         investigation_id=inv_id, signal_id=state["signal_id"], service=signal.get("service"),
         severity=signal.get("severity"), error=signal.get("error"))
    return {"investigation_id": inv_id}


def build():
    g = StateGraph(State)
    g.add_node("load", load)
    g.add_node("gather", gather)
    g.add_node("inspect_code", inspect_code)
    g.add_node("rca", rca)
    g.add_node("card", card)
    g.add_edge(START, "load")
    g.add_edge("load", "gather")
    g.add_edge("gather", "inspect_code")
    g.add_edge("inspect_code", "rca")
    g.add_edge("rca", "card")
    g.add_edge("card", END)
    return g.compile()


@on_approval(STAGE, extra_actions={"developer": "I'll fix this"})
def handle_act(item_id: str, action: str) -> str:
    """Slack gate handler (registered via load_all): approve queues the fix."""
    item = get_item(item_id)
    signal_id = (item["data"] if item else {}).get("signal_id", "?")
    bind_trace(signal_id if signal_id != "?" else None)
    sig = get_item(signal_id) if signal_id != "?" else None
    issue_id = (sig["data"] if sig else {}).get("issue_id")
    if action == "developer":
        issues.mark_developer_fix(issue_id)
        step(logger, "incident.05.gate", "developer will fix it themselves, coding agent not started",
             investigation_id=item_id, signal_id=signal_id, issue_id=issue_id)
        return (f"recorded (signal {signal_id}): a developer will fix this. The coding agent will not run; "
                f"you will get a reminder every {issues.remind_interval().total_seconds() / 3600:g} h until the "
                "error stops")
    if action == "approve":
        issues.mark_fix_pending(issue_id)
        update_item(item_id, fix_queued=True)
        step(logger, "incident.05.gate", "human APPROVED, fix queued", investigation_id=item_id,
             action=action, signal_id=signal_id)
        if (item["data"] if item else {}).get("fix_type", "code") == "code":
            from sre_agent.sre.auto import after_approval  # auto mode only; no-op unless SRE_AUTO_RUN=true
            after_approval(item_id)
        else:
            step(logger, "incident.05.gate", "ops-type fix: coding agent not started", investigation_id=item_id)
        if (item["data"] if item else {}).get("fix_type", "code") != "code":
            return (f"approved (signal {signal_id}): this is an operational fix, so no code change was "
                    "started - follow the NEXT STEPS in the incident email")
        from sre_agent.agents import is_enabled
        if not is_enabled("coding"):
            step(logger, "incident.05.gate", "coding agent disabled, nothing started",
                 level="warning", investigation_id=item_id)
            return f"approved (signal {signal_id}), but the coding agent is disabled (CODING_ENABLED=false)"
        return f"fix queued for coding agent (signal {signal_id})"
    issues.mark_rejected(issue_id)
    step(logger, "incident.05.gate", "human REJECTED fix", level="warning",
         investigation_id=item_id, action=action, signal_id=signal_id)
    return f"fix rejected by human (signal {signal_id})"


def run(signal_id: str = "") -> str:
    if not signal_id:
        raise ValueError("pass signal_id")
    out = build().invoke({"signal_id": signal_id})
    return (
        f"incident {out['investigation_id']}: {out['signal'].get('service', '?')} "
        f"{out['signal'].get('severity', '?')} {out['signal'].get('error', '?')} "
        f"-> approval gate open (rca {len(out['rca'])} chars)"
    )

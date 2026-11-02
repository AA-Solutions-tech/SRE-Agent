"""Supervisor: intake -> route (Clef, rules fallback) -> delegate -> notify.

Clef is the Cloudflare decision-making model; when it is unreachable,
unconfigured, or unsure, the deterministic rules router takes over and
the fallback is recorded in the result. Sub-agents are called only via
their public run(); removing an agent folder just shrinks the routable
set -- the supervisor never imports agent internals.

    python run.py supervisor --signal-id <id>
    python run.py supervisor --event '{"service":"api","severity":"HIGH","error":"boom"}'
    POST /run/supervisor  {"signal_id": "..."}  |  {"event": {...}}
"""
import os

from langgraph.graph import END, START, StateGraph
from typing import TypedDict

from sre_agent.core.db import create_item, get_item, list_items
from sre_agent.core.notify import notify
from sre_agent.core.sre_log import bind_trace, get_logger, step

logger = get_logger("supervisor")


def _approved_investigation(signal_id: str) -> dict | None:
    """Latest human-approved investigation for this signal (fix_queued set by the gate)."""
    for item in list_items(kind="investigation"):
        data = item["data"]
        if (isinstance(data, dict) and data.get("signal_id") == signal_id
                and data.get("fix_queued")):
            return item
    return None


def _delegate_coding(mod, signal: dict):
    """Coding needs incident context (it takes incident_id, not signal_id)."""
    inv = _approved_investigation(signal.get("id", ""))
    if inv is None:
        return "no approved investigation for this signal (approve the incident card first)"
    return mod.run(incident_id=inv["id"], signal=signal.get("id"), investigation=inv["id"])

from .router import Route, RouterError, enforce
from . import router_clef as clef
from . import router_rules as rules


class State(TypedDict, total=False):
    signal_id: str
    event: dict
    signal: dict
    route: Route
    router_used: str
    delegation: str
    summary: str


def intake(state: State) -> State:
    if state.get("signal_id"):
        item = get_item(state["signal_id"])
        if item is None or item["kind"] != "signal":
            raise ValueError(f"unknown signal: {state['signal_id']}")
        signal = dict(item["data"])
        signal["id"] = item["id"]
        bind_trace(item["id"])
        step(logger, "supervisor.01.intake", "signal loaded from DB", signal_id=item["id"],
             service=signal.get("service"), severity=signal.get("severity"), error=signal.get("error"))
        return {"signal": signal}
    event = state.get("event") or {}
    signal_id = create_item("signal", event)  # injected events become auditable records
    signal = dict(event)
    signal["id"] = signal_id
    bind_trace(signal_id)
    step(logger, "supervisor.01.intake", "injected event stored as signal", signal_id=signal_id,
         service=signal.get("service"), severity=signal.get("severity"), error=signal.get("error"))
    return {"signal": signal, "signal_id": signal_id}


def route(state: State) -> State:
    from sre_agent.agents import agent_names  # lazy: avoids circular import at discovery time

    available = [a for a in agent_names() if a in rules.CAPABLE]
    signal = state["signal"]
    bind_trace(signal.get("id"))
    which = os.getenv("SUPERVISOR_ROUTER", "clef").strip().lower()
    step(logger, "supervisor.02.route", "routing signal", router=which, available=available,
         signal_id=signal.get("id"))
    if which == "clef":
        try:
            decided = clef.decide(signal, available)
            if decided.get("confidence", 0.0) < clef.min_confidence():
                raise RouterError(
                    f"clef confidence {decided.get('confidence')} below minimum"
                )
            routed = enforce(decided, available)
            step(logger, "supervisor.02.route", "route decided", router="clef",
                 target=routed.get("agent"), confidence=decided.get("confidence"),
                 reason=routed.get("reason"), signal_id=signal.get("id"))
            return {"route": routed, "router_used": "clef"}
        except RouterError as e:
            fallback = rules.decide(signal, available)
            step(logger, "supervisor.02.route", "clef failed, using rules router", level="warning",
                 router="rules", target=fallback.get("agent"), reason=f"clef fallback: {e}",
                 signal_id=signal.get("id"))
            return {"route": fallback, "router_used": f"rules (clef fallback: {e})"}
    decided = rules.decide(signal, available)
    step(logger, "supervisor.02.route", "route decided", router="rules",
         target=decided.get("agent"), reason=decided.get("reason"), signal_id=signal.get("id"))
    return {"route": decided, "router_used": "rules"}


def delegate(state: State) -> State:
    from sre_agent.agents import load_agent  # lazy: avoids circular import at discovery time

    target = state["route"].get("agent")
    signal = state["signal"]
    bind_trace(signal.get("id"))
    if target is None:
        logger.warning("delegate.done", extra={"target": None, "reason": state["route"].get("reason"),
                                                "signal_id": signal.get("id")})
        return {
            "delegation": f"no capable agent installed ({state['route'].get('reason')}); "
            "install monitor, incident, or coding to handle this signal"
        }
    step(logger, "supervisor.03.delegate", "delegating to agent", target=target,
         signal_id=signal.get("id"))
    try:
        mod = load_agent(target)
        if target == "monitor":
            out = mod.run(source=signal.get("source", ""), mode="once")
        elif target == "coding":
            out = _delegate_coding(mod, signal)
        else:
            try:
                out = mod.run(signal_id=signal["id"])
            except TypeError:
                out = mod.run()  # agents that take no context (hello, manager_report)
    except Exception as e:
        out = f"{target} failed: {e}"
        step(logger, "supervisor.03.delegate", "delegation failed", level="error", target=target,
             error=f"{type(e).__name__}: {e}"[:300], signal_id=signal.get("id"))
        return {"delegation": f"{target}: {out}"}
    step(logger, "supervisor.03.delegate", "delegation done", target=target,
         outcome=str(out)[:200], signal_id=signal.get("id"))
    return {"delegation": f"{target}: {out}"}


def notify_step(state: State) -> State:
    signal = state["signal"]
    bind_trace(signal.get("id"))
    summary = (
        f"supervisor [{state['router_used']}] signal {state.get('signal_id', '?')} "
        f"({signal.get('service', '?')} {signal.get('severity', '?')} "
        f"{signal.get('error', '?')}) -> {state['delegation']}"
    )
    notify(f"sre supervisor: {signal.get('service', '?')} {signal.get('severity', '?')}", summary)
    step(logger, "supervisor.04.notify", "supervisor summary sent", summary=summary[:300])
    return {"summary": summary}


def build():
    g = StateGraph(State)
    g.add_node("intake", intake)
    g.add_node("route", route)
    g.add_node("delegate", delegate)
    g.add_node("notify_step", notify_step)
    g.add_edge(START, "intake")
    g.add_edge("intake", "route")
    g.add_edge("route", "delegate")
    g.add_edge("delegate", "notify_step")
    g.add_edge("notify_step", END)
    return g.compile()


def run(signal_id: str = "", event: dict | None = None) -> str:
    if not signal_id and event is None:
        raise ValueError("pass signal_id or event")
    return build().invoke({"signal_id": signal_id, "event": event or {}})["summary"]

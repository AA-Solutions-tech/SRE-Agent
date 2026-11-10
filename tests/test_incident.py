"""Incident (investigate) tests: monitor handoff, supervisor routing,
Slack card shape, approval gate. Offline: heuristic RCA, isolated DB."""
import pytest

from sre_agent import agents
from sre_agent.core.approvals import handle_decision
from sre_agent.core.db import get_item, list_items
from sre_agent.core.notify import approval_token
from sre_agent.sre.incident import agent as incident
from sre_agent.sre.incident import rca as rca_mod
from sre_agent.sre.monitor import agent as monitor_agent
from sre_agent.sre.supervisor import agent as supervisor


@pytest.fixture
def captured(monkeypatch):
    calls = []
    # request_approval sends the single card (with buttons) via core.approvals.notify
    monkeypatch.setattr("sre_agent.core.approvals.notify", lambda t, b, buttons=None: calls.append((t, b, buttons)))
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: None)
    monkeypatch.setattr("sre_agent.sre.supervisor.agent.notify", lambda *a, **k: None)
    return calls


@pytest.fixture
def installed(monkeypatch):
    def _set(*names):
        monkeypatch.setattr(agents, "agent_names", lambda: list(names))

    return _set


def _fire(tmp_path, lines="[2026-10-09T11:00:02Z] [ERROR] [payment-api] MongoNetworkTimeout after 3 retries\n"):
    p = tmp_path / "app.log"
    p.write_text(lines)
    out = monitor_agent.run(source=str(p), mode="once")
    assert out.startswith("signal ")
    return out.split()[1].rstrip(":")


# ---- monitor -> incident handoff (JSON leg) ----


def test_handoff_rca_links_signal(tmp_path, captured, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sid = _fire(tmp_path)
    out = incident.run(signal_id=sid)
    invs = list_items(kind="investigation")
    assert len(invs) == 1
    assert invs[0]["data"]["signal_id"] == sid
    assert invs[0]["data"]["error"] == "MongoNetworkTimeout"
    assert invs[0]["status"] == f"awaiting_{incident.STAGE}"
    assert f"incident {invs[0]['id']}" in out


def test_card_is_human_summary_not_json(tmp_path, captured):
    sid = _fire(tmp_path)
    incident.run(signal_id=sid)
    assert len(captured) == 1
    title, body, buttons = captured[0]
    assert "payment-api" in title and "MongoNetworkTimeout" in title
    assert "ROOT CAUSE" in body and "LOG EVIDENCE" in body
    assert '{"service"' not in body and '"evidence"' not in body
    assert len(buttons) == 5
    labels = [b[0] for b in buttons]
    assert labels == ["Approve", "Reject", "I'll fix this", "Ignore", "Ignore similar"]
    assert f"/approve/" in buttons[0][1] and "/" + incident.STAGE + "/approve?" in buttons[0][1]


def test_heuristic_fallback_without_llm(tmp_path, captured, monkeypatch):
    monkeypatch.setattr(incident, "get_llm", lambda **k: (_ for _ in ()).throw(RuntimeError("no key")))
    sid = _fire(tmp_path)
    incident.run(signal_id=sid)
    inv = list_items(kind="investigation")[0]
    assert "Likely cause" in inv["data"]["rca"]


def test_unknown_signal_raises():
    with pytest.raises(ValueError):
        incident.run(signal_id="nope")
    with pytest.raises(ValueError):
        incident.run()


# ---- approval gate ----


def test_gate_approve_and_reject(tmp_path, captured):
    sid = _fire(tmp_path)
    incident.run(signal_id=sid)
    inv_id = list_items(kind="investigation")[0]["id"]
    msg = handle_decision(inv_id, incident.STAGE, "approve", approval_token(inv_id, incident.STAGE))
    assert "fix queued" in msg
    assert get_item(inv_id)["status"] == f"{incident.STAGE}_approved"
    # single-use: second click rejected
    with pytest.raises(ValueError):
        handle_decision(inv_id, incident.STAGE, "approve", approval_token(inv_id, incident.STAGE))


def test_gate_reject(tmp_path, captured):
    sid = _fire(tmp_path)
    incident.run(signal_id=sid)
    inv_id = list_items(kind="investigation")[0]["id"]
    handle_decision(inv_id, incident.STAGE, "reject", approval_token(inv_id, incident.STAGE))
    # kit convention: status = stage + action + "d"
    assert get_item(inv_id)["status"] == f"{incident.STAGE}_rejectd"  # kit convention: stage + action + 'd'


def test_gate_bad_token_rejected(tmp_path, captured):
    sid = _fire(tmp_path)
    incident.run(signal_id=sid)
    inv_id = list_items(kind="investigation")[0]["id"]
    with pytest.raises(PermissionError):
        handle_decision(inv_id, incident.STAGE, "approve", "bad-token")


# ---- supervisor routing to incident ----


def test_supervisor_routes_high_to_incident(installed, captured, monkeypatch, tmp_path):
    installed("monitor", "incident")
    monkeypatch.setenv("SUPERVISOR_ROUTER", "rules")
    sid = _fire(tmp_path)
    out = supervisor.run(signal_id=sid)
    assert "[rules]" in out and "incident:" in out and "approval gate open" in out


def test_supervisor_skips_removed_incident(installed, captured, monkeypatch, tmp_path):
    installed("monitor")
    monkeypatch.setenv("SUPERVISOR_ROUTER", "rules")
    log = tmp_path / "app2.log"
    log.write_text("[2026-10-09T11:00:01Z] [ERROR] [payment-api] boom\n")
    from sre_agent.core.db import create_item

    sid = create_item("signal", {"service": "payment-api", "severity": "HIGH",
                                 "error": "boom", "evidence": ["x"],
                                 "ts": "2026-10-09T11:00:01Z", "source": str(log)})
    assert "monitor:" in supervisor.run(signal_id=sid)


def test_rca_prompt_is_grounded():
    p = rca_mod.build_rca_prompt({"service": "s", "error": "e"}, [])
    assert "Do not invent" in p or "ONLY the evidence" in p


def test_malformed_signal_record_does_not_break_gather(tmp_path, captured):
    from sre_agent.core.db import create_item

    create_item("signal", '{"service":"payment-api"}')  # double-encoded junk (legacy CLI bug)
    sid = _fire(tmp_path)
    out = incident.run(signal_id=sid)
    assert "approval gate open" in out


# ---- developer self-fix + issue lifecycle ----
def test_developer_action_marks_issue_and_skips_coding(tmp_path, captured, monkeypatch):
    from sre_agent.sre import issues

    started = []
    monkeypatch.setattr("sre_agent.sre.auto.after_approval", lambda i: started.append(i))
    sid = _fire(tmp_path)
    incident.run(signal_id=sid)
    inv = list_items(kind="investigation")[0]
    issue_id = get_item(sid)["data"]["issue_id"]
    msg = handle_decision(inv["id"], incident.STAGE, "developer", approval_token(inv["id"], incident.STAGE))
    assert "developer will fix" in msg and not started
    assert get_item(issue_id)["data"]["state"] == "developer_fix"
    assert get_item(inv["id"])["status"] == f"{incident.STAGE}_developerd"
    assert issues.get_issue(issue_id)["data"]["investigation_id"] == inv["id"]


def test_approve_and_reject_move_the_issue(tmp_path, captured):
    sid = _fire(tmp_path)
    incident.run(signal_id=sid)
    inv_id = list_items(kind="investigation")[0]["id"]
    issue_id = get_item(sid)["data"]["issue_id"]
    handle_decision(inv_id, incident.STAGE, "approve", approval_token(inv_id, incident.STAGE))
    assert get_item(issue_id)["data"]["state"] == "fix_pending"

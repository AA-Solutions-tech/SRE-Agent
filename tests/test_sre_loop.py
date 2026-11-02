"""SRE loop integration tests: monitor -> supervisor -> incident -> gate.

One file proving the whole chain on real records (isolated DB, offline):
a log ERROR becomes a signal, the supervisor routes it to incident,
incident files an RCA + gated Slack card, and approval closes the loop.
"""
import pytest

from sre_agent import agents
from sre_agent.core.approvals import handle_decision
from sre_agent.core.db import get_item, list_items
from sre_agent.core.notify import approval_token
from sre_agent.sre.incident import agent as incident
from sre_agent.sre.monitor import agent as monitor
from sre_agent.sre.supervisor import agent as supervisor


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: None)
    monkeypatch.setattr("sre_agent.sre.supervisor.agent.notify", lambda *a, **k: None)
    monkeypatch.setattr("sre_agent.core.approvals.notify", lambda *a, **k: None)
    monkeypatch.setenv("SUPERVISOR_ROUTER", "rules")
    monkeypatch.setattr(agents, "agent_names", lambda: ["monitor", "incident", "supervisor"])
    return None


def _write_log(tmp_path, *lines):
    p = tmp_path / "app.log"
    p.write_text("".join(lines))
    return str(p)


def test_loop_monitor_to_approved_fix(tmp_path, quiet):
    # 1. monitor watches the log and files a signal
    log = _write_log(
        tmp_path,
        "[2026-10-09T11:00:01Z] [INFO] [payment-api] request ok\n",
        "[2026-10-09T11:00:02Z] [ERROR] [payment-api] MongoNetworkTimeout after 3 retries\n",
    )
    monitor_out = monitor.run(source=log, mode="once")
    assert monitor_out.startswith("signal ")
    signal_id = monitor_out.split()[1].rstrip(":")
    signal = get_item(signal_id)["data"]
    assert (signal["service"], signal["severity"], signal["error"]) == (
        "payment-api", "HIGH", "MongoNetworkTimeout")

    # 2. supervisor routes the signal to incident
    super_out = supervisor.run(signal_id=signal_id)
    assert "[rules]" in super_out and "incident:" in super_out

    # 3. incident filed an RCA investigation with an open gate
    invs = list_items(kind="investigation")
    assert len(invs) == 1
    inv = invs[0]
    assert inv["data"]["signal_id"] == signal_id
    assert inv["status"] == f"awaiting_{incident.STAGE}"
    assert "MongoNetworkTimeout" in inv["data"]["rca"]

    # 4. human approves -> fix queued for the coding agent
    msg = handle_decision(inv["id"], incident.STAGE, "approve",
                          approval_token(inv["id"], incident.STAGE))
    assert "fix queued" in msg
    assert get_item(inv["id"])["status"] == f"{incident.STAGE}_approved"


def test_loop_reverify_clean_log(tmp_path, quiet):
    # after a fix, the monitor re-checks the same source: clean log, no new signal
    log = _write_log(tmp_path, "[2026-10-09T12:00:01Z] [INFO] [payment-api] all good\n")
    assert "no new signals" in monitor.run(source=log, mode="once")
    assert list_items(kind="signal") == []


def test_loop_survives_removed_incident(tmp_path, quiet, monkeypatch):
    # incident folder gone -> supervisor still completes via monitor
    monkeypatch.setattr(agents, "agent_names", lambda: ["monitor", "supervisor"])
    log = _write_log(tmp_path, "[2026-10-09T11:00:01Z] [ERROR] [payment-api] boom\n")
    sid = monitor.run(source=log, mode="once").split()[1].rstrip(":")
    out = supervisor.run(signal_id=sid)
    assert "monitor:" in out and "incident:" not in out

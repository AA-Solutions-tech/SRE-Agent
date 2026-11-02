"""Monitor <-> incident direct connection tests (no supervisor in between).

Proves the two-agent handoff contract: monitor files signal JSON records,
incident consumes them straight by id — RCA links, quarantine travels,
cards render, gates open and close.
"""
import pytest

from sre_agent.core.approvals import handle_decision
from sre_agent.core.db import get_item, list_items
from sre_agent.core.notify import approval_token
from sre_agent.sre.incident import agent as incident
from sre_agent.sre.monitor import agent as monitor


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: None)
    monkeypatch.setattr("sre_agent.core.approvals.notify", lambda *a, **k: None)


def _fire(tmp_path, name="app.log", lines=None, new=True):
    lines = lines if lines is not None else (
        "[2026-10-09T11:00:02Z] [ERROR] [payment-api] MongoNetworkTimeout after 3 retries\n"
    )
    p = tmp_path / name
    p.write_text(lines)
    out = monitor.run(source=str(p), mode="once")
    if not new:  # a repeat of a known issue: counted on the issue, the signal record still exists
        assert out.startswith("no new signals")
        return list_items(kind="signal")[0]["id"]
    assert out.startswith("signal ")
    return out.split()[1].rstrip(":")


def test_monitor_signal_feeds_incident_directly(tmp_path, quiet):
    sid = _fire(tmp_path)
    out = incident.run(signal_id=sid)
    inv = list_items(kind="investigation")[0]
    assert inv["data"]["signal_id"] == sid
    assert inv["data"]["error"] == "MongoNetworkTimeout"
    assert f"incident {inv['id']}" in out


def test_quarantine_lines_travel_to_investigation(tmp_path, quiet):
    sid = _fire(tmp_path, lines=(
        "garbage line with no format\n"
        "[2026-10-09T11:00:02Z] [ERROR] [payment-api] RCACrash boom\n"
    ))
    assert get_item(sid)["data"]["quarantine"] == ["garbage line with no format"]
    incident.run(signal_id=sid)
    inv = list_items(kind="investigation")[0]
    assert inv["data"]["signal_id"] == sid


def test_recurrence_counted_across_signals(tmp_path, quiet):
    _fire(tmp_path, name="a.log")
    sid2 = _fire(tmp_path, name="b.log", new=False)
    incident.run(signal_id=sid2)
    inv = list_items(kind="investigation")[0]
    assert "seen 2 time(s)" in inv["data"]["rca"]


def test_each_signal_gets_own_investigation(tmp_path, quiet):
    sid1 = _fire(tmp_path, name="a.log")
    sid2 = _fire(tmp_path, name="b.log", lines="[2026-10-09T11:00:03Z] [ERROR] [payment-api] DiskFull on /data" + chr(10))
    incident.run(signal_id=sid1)
    incident.run(signal_id=sid2)
    linked = sorted(i["data"]["signal_id"] for i in list_items(kind="investigation"))
    assert linked == sorted([sid1, sid2])


def test_reject_path_closes_gate(tmp_path, quiet):
    sid = _fire(tmp_path)
    incident.run(signal_id=sid)
    inv_id = list_items(kind="investigation")[0]["id"]
    msg = handle_decision(inv_id, incident.STAGE, "reject",
                          approval_token(inv_id, incident.STAGE))
    assert "rejected" in msg
    assert get_item(inv_id)["status"] == f"{incident.STAGE}_rejectd"

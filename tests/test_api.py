"""API endpoint tests (in-process TestClient, isolated SQLite DB)."""
import pytest
from fastapi.testclient import TestClient

from sre_agent import server
from sre_agent.sre.supervisor import agent as supervisor_agent

client = TestClient(server.app)


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(supervisor_agent, "notify", lambda *a, **k: None)
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: None)
    monkeypatch.setenv("SUPERVISOR_ROUTER", "rules")


def _event(**kw):
    base = {"service": "payment-api", "severity": "HIGH", "error": "boom"}
    base.update(kw)
    return base


def test_health_lists_agents():
    r = client.get("/health")
    assert r.status_code == 200
    assert "supervisor" in r.json()["agents"]


def test_agents_list_has_descriptions():
    r = client.get("/agents")
    names = {a["name"]: a["description"] for a in r.json()["agents"]}
    assert "monitor" in names and names["monitor"]
    assert "supervisor" in names and names["supervisor"]


def test_signal_inject_and_read():
    sid = client.post("/signals", json=_event()).json()["signal_id"]
    assert client.get(f"/signals/{sid}").json()["data"]["error"] == "boom"
    ids = [s["id"] for s in client.get("/signals").json()["signals"]]
    assert sid in ids
    assert client.get("/signals/nope").status_code == 404


def test_signals_filter():
    client.post("/signals", json=_event(severity="HIGH"))
    client.post("/signals", json=_event(severity="LOW"))
    highs = client.get("/signals", params={"severity": "HIGH"}).json()["signals"]
    assert highs and all(s["data"]["severity"] == "HIGH" for s in highs)
    svc = client.get("/signals", params={"service": "payment-api"}).json()["signals"]
    assert len(svc) >= 2
    assert client.get("/signals", params={"service": "nobody"}).json()["signals"] == []


def test_signal_inject_and_supervise(quiet):
    r = client.post("/signals", params={"supervise": "true"}, json=_event())
    assert r.status_code == 200
    body = r.json()
    assert "signal_id" in body and "incident:" in body["result"]


def test_run_unknown_agent_404():
    assert client.post("/run/nope", json={}).status_code == 404


def test_run_bad_reference_404_not_500():
    r = client.post("/run/incident", json={"signal_id": "nope"})
    assert r.status_code == 404

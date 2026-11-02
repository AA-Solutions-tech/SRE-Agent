"""Supervisor tests (offline: mocked Clef HTTP, isolated SQLite DB)."""
import pytest

from sre_agent import agents
from sre_agent.sre.supervisor import agent as supervisor
from sre_agent.sre.supervisor import router_clef as clef
from sre_agent.sre.supervisor import router_rules as rules
from sre_agent.sre.supervisor.router import RouterError, enforce
from sre_agent.core.db import create_item


@pytest.fixture
def notified(monkeypatch):
    calls = []
    monkeypatch.setattr(
        supervisor, "notify", lambda t, b, buttons=None: calls.append((t, b))
    )
    monkeypatch.setattr(
        "sre_agent.sre.monitor.agent.notify", lambda t, b, buttons=None: None
    )
    return calls


@pytest.fixture
def installed(monkeypatch):
    """Pretend only these agents are installed (tests the removal matrix)."""

    def _set(*names):
        monkeypatch.setattr(agents, "agent_names", lambda: list(names))

    return _set


def _signal(**kw):
    base = {
        "service": "payment-api",
        "severity": "HIGH",
        "error": "MongoNetworkTimeout",
        "evidence": ["e1"],
        "ts": "2026-10-09T11:00:01Z",
        "source": "/tmp/x.log",
    }
    base.update(kw)
    return base


# ---- rules router ----


def test_rules_high_prefers_incident(installed):
    installed("monitor", "incident", "coding")
    assert rules.decide(_signal(), ["monitor", "incident", "coding"])["agent"] == "incident"


def test_rules_uses_only_installed(installed):
    installed("monitor")
    r = rules.decide(_signal(), ["monitor"])
    assert (r["agent"], r["confidence"]) == ("monitor", 1.0)


def test_rules_none_capable(installed):
    installed("hello")
    r = rules.decide(_signal(), ["hello"])
    assert r["agent"] is None


def test_enforce_blocks_removed_agent():
    r = enforce({"agent": "coding", "confidence": 0.9, "reason": "x"}, ["monitor"])
    assert r["agent"] is None and "not installed" in r["reason"]


# ---- clef adapter (mocked HTTP, Workers AI envelope) ----


def _env(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setenv("CLEF_MODEL", "@cf/cloudflare/clef")


def _ok(reply):
    return {"result": {"response": reply}, "success": True}


class _FakeHttpx:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.seen = {}

    def post(self, url, json=None, headers=None, timeout=None):
        self.seen = {"url": url, "json": json, "headers": headers}
        if self.error:
            raise self.error
        return self

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_clef_parses_workers_ai_reply(monkeypatch):
    _env(monkeypatch)
    fake = _FakeHttpx(_ok('{"agent": "incident", "confidence": 0.8, "reason": "db errors"}'))
    monkeypatch.setattr(clef, "httpx", fake)
    r = clef.decide(_signal(), ["monitor", "incident"])
    assert (r["agent"], r["confidence"]) == ("incident", 0.8)
    assert "/accounts/acct/ai/run/" in fake.seen["url"]
    assert fake.seen["headers"]["Authorization"] == "Bearer tok"
    assert "payment-api" in fake.seen["json"]["prompt"]


def test_clef_parses_reply_with_prose(monkeypatch):
    _env(monkeypatch)
    fake = _FakeHttpx(_ok('Sure.\n{"agent": null, "confidence": 0.9, "reason": "no"} done'))
    monkeypatch.setattr(clef, "httpx", fake)
    assert clef.decide(_signal(), ["monitor"])["agent"] is None


def test_clef_transport_error_raises(monkeypatch):
    _env(monkeypatch)
    fake = _FakeHttpx(error=ConnectionError("down"))
    monkeypatch.setattr(clef, "httpx", fake)
    with pytest.raises(RouterError):
        clef.decide(_signal(), ["monitor"])


def test_clef_error_envelope_raises(monkeypatch):
    _env(monkeypatch)
    fake = _FakeHttpx({"success": False, "errors": [{"code": 7000, "message": "nope"}]})
    monkeypatch.setattr(clef, "httpx", fake)
    with pytest.raises(RouterError):
        clef.decide(_signal(), ["monitor"])


def test_clef_non_json_reply_raises(monkeypatch):
    _env(monkeypatch)
    fake = _FakeHttpx(_ok("absolutely the monitor, trust me"))
    monkeypatch.setattr(clef, "httpx", fake)
    with pytest.raises(RouterError):
        clef.decide(_signal(), ["monitor"])


def test_clef_missing_config_raises(monkeypatch):
    for k in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "CLEF_MODEL"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RouterError):
        clef.decide(_signal(), ["monitor"])


# ---- end to end ----


def test_run_routes_and_delegates(installed, notified, monkeypatch, tmp_path):
    installed("monitor")
    monkeypatch.setenv("SUPERVISOR_ROUTER", "rules")
    log = tmp_path / "app.log"
    log.write_text("[2026-10-09T11:00:01Z] [ERROR] [payment-api] boom\n")
    sid = create_item("signal", _signal(source=str(log)))
    out = supervisor.run(signal_id=sid)
    assert "monitor:" in out and "[rules]" in out
    assert len(notified) == 1 and "payment-api" in notified[0][0]


def test_run_clef_low_confidence_falls_back(installed, notified, monkeypatch):
    installed("monitor")
    _env(monkeypatch)
    monkeypatch.setenv("SUPERVISOR_ROUTER", "clef")
    fake = _FakeHttpx(_ok('{"agent": "coding", "confidence": 0.1, "reason": "unsure"}'))
    monkeypatch.setattr(clef, "httpx", fake)
    sid = create_item("signal", _signal())
    out = supervisor.run(signal_id=sid)
    assert "rules (clef fallback" in out and "monitor:" in out


def test_run_clef_error_falls_back(installed, notified, monkeypatch):
    installed("monitor")
    _env(monkeypatch)
    monkeypatch.setenv("SUPERVISOR_ROUTER", "clef")
    fake = _FakeHttpx(error=TimeoutError("slow"))
    monkeypatch.setattr(clef, "httpx", fake)
    sid = create_item("signal", _signal())
    assert "rules (clef fallback" in supervisor.run(signal_id=sid)


def test_run_clef_first_by_default(installed, notified, monkeypatch):
    installed("monitor", "incident")
    _env(monkeypatch)
    monkeypatch.delenv("SUPERVISOR_ROUTER", raising=False)  # default path
    fake = _FakeHttpx(_ok('{"agent": "incident", "confidence": 0.9, "reason": "db"}'))
    monkeypatch.setattr(clef, "httpx", fake)
    sid = create_item("signal", _signal())
    out = supervisor.run(signal_id=sid)
    assert "[clef]" in out and "incident:" in out


def test_run_no_capable_agent_notice(installed, notified, monkeypatch):
    installed("hello")
    monkeypatch.setenv("SUPERVISOR_ROUTER", "rules")
    sid = create_item("signal", _signal())
    out = supervisor.run(signal_id=sid)
    assert "no capable agent installed" in out


def test_run_injected_event_becomes_signal(installed, notified, monkeypatch):
    installed("hello")
    monkeypatch.setenv("SUPERVISOR_ROUTER", "rules")
    out = supervisor.run(event=_signal())
    assert "no capable agent installed" in out
    from sre_agent.core.db import list_items

    assert len(list_items(kind="signal")) == 1


def test_run_unknown_signal_raises(installed, monkeypatch):
    installed("monitor")
    monkeypatch.setenv("SUPERVISOR_ROUTER", "rules")
    with pytest.raises(ValueError):
        supervisor.run(signal_id="nope")


def test_run_needs_input():
    with pytest.raises(ValueError):
        supervisor.run()


# ---- supervisor -> coding delegation ----


class _FakeCoding:
    def __init__(self):
        self.seen = None

    def run(self, **kwargs):
        self.seen = kwargs
        return {"incident_id": kwargs.get("incident_id"), "status": "completed", "summary": "ok"}


def test_delegate_coding_receives_approved_context(installed, notified, monkeypatch):
    installed("coding")
    monkeypatch.setenv("SUPERVISOR_ROUTER", "rules")
    sid = create_item("signal", _signal())
    inv_id = create_item("investigation", {"signal_id": sid, "fix_queued": True})
    fake = _FakeCoding()
    monkeypatch.setattr(agents, "load_agent", lambda name: fake)
    out = supervisor.run(signal_id=sid)
    assert fake.seen == {"incident_id": inv_id, "signal": sid, "investigation": inv_id}
    assert "coding:" in out


def test_delegate_coding_waits_for_approval(installed, notified, monkeypatch):
    installed("coding")
    monkeypatch.setenv("SUPERVISOR_ROUTER", "rules")
    sid = create_item("signal", _signal())
    create_item("investigation", {"signal_id": sid})  # not approved: no fix_queued
    runs = []

    class _SpyCoding(_FakeCoding):
        def run(self, **kwargs):
            runs.append(kwargs)
            return super().run(**kwargs)

    monkeypatch.setattr(agents, "load_agent", lambda name: _SpyCoding())
    out = supervisor.run(signal_id=sid)
    assert runs == [] and "no approved investigation" in out

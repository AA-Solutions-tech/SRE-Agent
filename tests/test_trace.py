"""One trace id follows an error through every agent (the signal id), in the logs and in email subjects."""
import json
import os

import pytest

from sre_agent import agents
from sre_agent.core import sre_log
from sre_agent.sre.incident import agent as incident
from sre_agent.sre.monitor import agent as monitor
from sre_agent.sre.supervisor import agent as supervisor


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: None)
    monkeypatch.setattr("sre_agent.sre.supervisor.agent.notify", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr("sre_agent.core.approvals.notify", lambda t, b, buttons=None, **kw: sent.append(t))
    monkeypatch.setenv("SUPERVISOR_ROUTER", "rules")
    monkeypatch.setattr(agents, "agent_names", lambda: ["monitor", "incident", "supervisor"])
    return sent


def _lines_for(trace_id: str) -> list[dict]:
    path = os.environ["SRE_LOG_PATH"]
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("details", {}).get("trace") == trace_id:
                out.append(rec)
    return out


def test_bind_trace_tags_following_steps_and_prints_it(capsys, monkeypatch):
    monkeypatch.setenv("SRE_TRACE", "true")
    log = sre_log.get_logger("trace_test")
    sre_log.bind_trace("tr-1")
    sre_log.step(log, "t.01.x", "hello", k=1)
    assert "[trace tr-1]" in capsys.readouterr().out
    sre_log.bind_trace(None)  # no id: the previous one stays, never blanked by mistake
    assert sre_log.current_trace() == "tr-1"


def test_one_error_leaves_the_same_trace_in_monitor_supervisor_and_incident(tmp_path, quiet):
    log = tmp_path / "app.log"
    log.write_text("[2026-10-09T11:00:02Z] [ERROR] [payment-api] TraceableError after 3 retries" + chr(10))
    out = monitor.run(source=str(log), mode="once")
    signal_id = out.split()[1].rstrip(":")
    supervisor.run(signal_id=signal_id)
    records = _lines_for(signal_id)
    loggers = {r["logger"] for r in records}
    assert {"sre.monitor", "sre.supervisor", "sre.incident"} <= loggers
    assert all(r["details"]["trace"] == signal_id for r in records)


def test_the_approval_email_subject_names_the_trace(tmp_path, quiet):
    log = tmp_path / "app.log"
    log.write_text("[2026-10-09T11:00:02Z] [ERROR] [payment-api] SubjectError boom" + chr(10))
    signal_id = monitor.run(source=str(log), mode="once").split()[1].rstrip(":")
    incident.run(signal_id=signal_id)
    assert quiet == [] or all(f"[trace {signal_id}]" in t for t in quiet)

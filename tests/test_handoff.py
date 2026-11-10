"""A signal that was stored but never investigated is retried and reported. Offline."""
import pytest

from sre_agent import agents
from sre_agent.core.db import create_item, get_item
from sre_agent.sre import auto, issues


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("HANDOFF_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("HANDOFF_RETRY_MINUTES", "0")
    alerts = []
    monkeypatch.setattr("sre_agent.sre.health.alert", lambda key, title, detail, **kw: alerts.append((key, title, detail)) or True)
    mode = {"v": "crash"}

    class Supervisor:
        @staticmethod
        def run(signal_id=""):
            if mode["v"] == "crash":
                raise RuntimeError("llm exploded")
            if mode["v"] == "ok":
                issue_id = get_item(signal_id)["data"]["issue_id"]
                issues.attach_investigation(issue_id, "inv1")
            return "supervisor: incident failed: boom" if mode["v"] == "swallow" else "supervisor: incident: card sent"

    monkeypatch.setattr(agents, "agent_names", lambda: ["supervisor"])
    monkeypatch.setattr(agents, "load_agent", lambda name: Supervisor)

    sid = create_item("signal", {"service": "api", "error": "TypeError: boom", "severity": "HIGH"})
    issue_id, _ = issues.record_occurrence({"service": "api", "error": "TypeError: boom", "severity": "HIGH"}, sid)
    from sre_agent.core.db import update_item

    update_item(sid, issue_id=issue_id)
    return sid, mode, alerts


def test_a_crash_is_recorded_reported_and_retried_until_it_works(world):
    sid, mode, alerts = world
    auto._on_signal(sid)
    item = get_item(sid)
    assert item["status"] == "handoff_failed" and item["data"]["handoff_attempts"] == 1
    assert "llm exploded" in item["data"]["handoff_error"]
    assert [a[0] for a in alerts] == [f"handoff:{sid}:first"] and "Retrying automatically" in alerts[0][2]

    mode["v"] = "ok"
    assert auto.retry_handoffs() == [sid]
    item = get_item(sid)
    assert item["status"] == "handed_off" and item["data"]["handoff_failed"] is False
    assert auto.retry_handoffs() == []  # nothing left to retry


def test_gives_up_after_max_attempts_and_says_so(world):
    sid, mode, alerts = world
    auto._on_signal(sid)
    auto.retry_handoffs()
    auto.retry_handoffs()
    item = get_item(sid)
    assert item["status"] == "handoff_gave_up" and item["data"]["handoff_attempts"] == 3
    assert alerts[-1][0] == f"handoff:{sid}:final" and "gave up" in alerts[-1][1]
    assert auto.retry_handoffs() == []  # no more attempts


def test_a_supervisor_that_swallows_the_failure_is_still_caught(world):
    sid, mode, alerts = world
    mode["v"] = "swallow"
    auto._on_signal(sid)
    assert get_item(sid)["status"] == "handoff_failed"
    assert "no investigation was produced" in get_item(sid)["data"]["handoff_error"]


def test_missing_supervisor_is_reported(world, monkeypatch):
    sid, mode, alerts = world
    monkeypatch.setattr(agents, "agent_names", lambda: [])
    auto._on_signal(sid)
    assert "not installed" in get_item(sid)["data"]["handoff_error"] and alerts


def test_success_leaves_no_failure_marks(world):
    sid, mode, alerts = world
    mode["v"] = "ok"
    auto._on_signal(sid)
    assert get_item(sid)["status"] == "handed_off" and not alerts

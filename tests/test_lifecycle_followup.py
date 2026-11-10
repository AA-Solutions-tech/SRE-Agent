"""Unanswered cards, escalation, verified-fixed messages and 'fix did not hold'. Offline."""
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.core.db import get_item, update_item
from sre_agent.sre import auto, issues

NOW = datetime.now(timezone.utc)


def _sig(error="TypeError: boom"):
    return {"service": "api", "severity": "HIGH", "error": error, "evidence": ["x"]}


def _carded(error="TypeError: boom", waiting_hours=5.0):
    """An open issue whose card went out `waiting_hours` ago and is still happening."""
    issue_id, _ = issues.record_occurrence(_sig(error), "s1")
    issues.attach_investigation(issue_id, "inv1")
    update_item(issue_id, state_since=(NOW - timedelta(hours=waiting_hours)).isoformat(),
                last_seen=NOW.isoformat())
    return issue_id


def test_unanswered_card_is_reminded_once_per_interval():
    issue_id = _carded()
    due = issues.sweep_all(NOW)["unanswered"]
    assert [d["id"] for d in due] == [issue_id] and due[0]["waiting_hours"] == pytest.approx(5, abs=0.1)
    assert due[0]["escalate"] is False
    assert issues.sweep_all(NOW)["unanswered"] == []  # not again until the interval passes
    later = NOW + timedelta(hours=5)
    update_item(issue_id, last_seen=later.isoformat())  # still failing then
    assert [d["id"] for d in issues.sweep_all(later)["unanswered"]] == [issue_id]


def test_a_fresh_card_is_not_nagged():
    _carded(waiting_hours=1)
    assert issues.sweep_all(NOW)["unanswered"] == []


def test_answered_cards_are_not_reminded_as_unanswered():
    issue_id = _carded()
    issues.mark_ignored(issue_id)
    assert issues.sweep_all(NOW)["unanswered"] == []


def test_escalation_fires_once_after_the_configured_time(monkeypatch):
    monkeypatch.setenv("ESCALATE_AFTER_HOURS", "8")
    issue_id = _carded(waiting_hours=5)
    assert issues.sweep_all(NOW)["unanswered"][0]["escalate"] is False
    update_item(issue_id, state_since=(NOW - timedelta(hours=9)).isoformat(),
                last_card_reminder=(NOW - timedelta(hours=5)).isoformat())
    assert issues.sweep_all(NOW)["unanswered"][0]["escalate"] is True
    update_item(issue_id, last_card_reminder=(NOW - timedelta(hours=5)).isoformat())
    assert issues.sweep_all(NOW)["unanswered"][0]["escalate"] is False  # only once


def test_open_issue_that_stops_by_itself_is_closed_and_reported_as_verified():
    issue_id = _carded()
    update_item(issue_id, last_seen=(NOW - timedelta(hours=2)).isoformat())
    res = issues.sweep_all(NOW)
    assert [(v["id"], v["was"]) for v in res["verified"]] == [(issue_id, "open")]
    assert get_item(issue_id)["data"]["state"] == "fixed"


def test_approved_fix_that_goes_quiet_is_verified():
    issue_id, _ = issues.record_occurrence(_sig(), "s1")
    issues.mark_fix_pending(issue_id)
    res = issues.sweep_all(NOW + timedelta(hours=2))
    assert [(v["id"], v["was"]) for v in res["verified"]] == [(issue_id, "fix_pending")]


def test_old_sweep_api_still_returns_only_reminders():
    _carded()
    assert issues.sweep(NOW) == []


def test_fix_that_did_not_hold_is_marked_on_the_next_card():
    issue_id, _ = issues.record_occurrence(_sig(), "s1")
    issues.mark_fix_pending(issue_id)
    issues.mark_fixed(issue_id)
    _, action = issues.record_occurrence(_sig(), "s2")
    d = get_item(issue_id)["data"]
    assert action == "investigate" and d["reopened_after_fix_min"] == 0
    assert "did not hold" in issues.describe(d)
    issues.mark_fixed(issue_id)  # fixed again: the note goes away
    assert "did not hold" not in issues.describe(get_item(issue_id)["data"])


def test_messages(monkeypatch):
    sent, escalated = [], []
    monkeypatch.setattr("sre_agent.sre.auto.notify", lambda t, b, buttons=None: sent.append((t, b)))
    monkeypatch.setattr("sre_agent.core.paging.escalate", lambda t, b: escalated.append(t) or True)
    issue = {"id": "i1", "service": "api", "error": "TypeError: boom", "severity": "HIGH", "count": 4,
             "waiting_hours": 6.2, "escalate": True, "investigation_id": None, "was": "fix_pending"}
    auto.send_unanswered(issue)
    assert sent[0][0].startswith("reminder: api TypeError: boom is waiting for a decision")
    assert "6.2 h" in sent[0][1] and escalated and escalated[0].startswith("ESCALATION:")
    auto.send_verified(issue)
    assert sent[1][0] == "recovered: api HIGH TypeError: boom" and "after the fix was approved" in sent[1][1]
    issue["escalate"] = False
    escalated.clear()
    auto.send_unanswered(issue)
    assert not escalated

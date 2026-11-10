"""Jobs that start but never finish, finish partially, and mixed-category batches. Offline."""
import json
from datetime import datetime, timedelta

import pytest

from sre_agent.core.db import list_items
from sre_agent.sre import classify, schedules
from sre_agent.sre.incident import agent as incident

MONDAY_9 = datetime(2026, 11, 9, 9, 0).astimezone()
START = "{t:%Y-%m-%dT%H:%M:%S} [INFO] [timesheet] timesheet reminder job started"
DONE = "{t:%Y-%m-%dT%H:%M:%S} [INFO] [timesheet] timesheet reminders sent {n} of {total}"


@pytest.fixture
def job(tmp_path, monkeypatch):
    f = tmp_path / "jobs.json"
    f.write_text(json.dumps({"jobs": [{
        "name": "weekly-timesheet", "service": "timesheet", "cron": "0 9 * * 1",
        "match": "timesheet reminder job started", "grace_minutes": 15,
        "done_match": r"reminders sent (?P<done>\d+) of (?P<total>\d+)", "done_regex": True, "max_minutes": 30}]}))
    monkeypatch.setenv("SRE_JOBS_FILE", str(f))
    monkeypatch.setattr("sre_agent.sre.schedules.notify", lambda *a, **k: None)
    schedules.observe([])
    schedules.check_missed(now=MONDAY_9 - timedelta(days=1))  # armed before it was due


def _signal(sid):
    return next(i for i in list_items(kind="signal") if i["id"] == sid)["data"]


def test_started_but_never_finished(job):
    t = MONDAY_9 + timedelta(minutes=1)
    schedules.observe([START.format(t=t)], now=t)
    assert schedules.check_missed(now=t + timedelta(minutes=10)) == []  # still within max_minutes
    filed = schedules.check_missed(now=t + timedelta(minutes=45))
    assert len(filed) == 1
    d = _signal(filed[0])
    assert d["error"] == "Scheduled job did not finish: weekly-timesheet" and d["details"]["kind"] == "unfinished"
    assert schedules.check_missed(now=t + timedelta(hours=3)) == []  # once per run


def test_finishing_in_time_is_fine(job):
    t = MONDAY_9 + timedelta(minutes=1)
    schedules.observe([START.format(t=t), DONE.format(t=t + timedelta(minutes=5), n=10, total=10)],
                      now=t + timedelta(minutes=6))
    assert schedules.check_missed(now=t + timedelta(hours=2)) == []


def test_finished_but_only_part_of_the_work(job):
    t = MONDAY_9 + timedelta(minutes=1)
    schedules.observe([START.format(t=t), DONE.format(t=t + timedelta(minutes=5), n=3, total=10)],
                      now=t + timedelta(minutes=6))
    filed = schedules.check_missed(now=t + timedelta(minutes=7))
    assert len(filed) == 1
    d = _signal(filed[0])
    assert d["error"] == "Scheduled job finished partially: weekly-timesheet"
    assert d["details"]["done"] == 3 and d["details"]["total"] == 10
    assert schedules.check_missed(now=t + timedelta(minutes=8)) == []


def test_cards_for_unfinished_and_partial_runs_say_what_happened(job, monkeypatch):
    cards = []
    monkeypatch.setattr("sre_agent.core.approvals.notify", lambda t, b, buttons=None: cards.append((t, b, buttons)))
    t = MONDAY_9 + timedelta(minutes=1)
    schedules.observe([START.format(t=t), DONE.format(t=t + timedelta(minutes=5), n=3, total=10)],
                      now=t + timedelta(minutes=6))
    incident.run(signal_id=schedules.check_missed(now=t + timedelta(minutes=7))[0])
    title, body, buttons = cards[0]
    assert title.startswith("action needed: timesheet HIGH") and "3 of 10" in body
    assert [b[0] for b in buttons] == ["I'll fix this", "Ignore", "Ignore similar"]


# ---- mixed batches ----


def test_a_mixed_batch_is_labelled_by_what_most_lines_say():
    sig = {"service": "api", "error": "x", "evidence": [
        "[ERROR] request failed: 429 Too Many Requests", "[ERROR] rate limit exceeded for key",
        "[ERROR] rate limit exceeded again", "[ERROR] connect ECONNREFUSED 127.0.0.1:5432"]}
    found = classify.classify(sig)
    assert found["category"] == "rate_limit" and found["details"]["mixed"] is True


def test_json_log_lines_are_classified_by_their_message():
    sig = {"service": "api", "error": "x", "evidence": [
        json.dumps({"level": "error", "message": "SMTP_PASSWORD is not set", "stack": "forbidden unauthorized"})]}
    assert classify.classify(sig)["category"] == "config"  # the stack text does not vote

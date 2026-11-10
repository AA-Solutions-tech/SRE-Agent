"""Scheduled-job watch: cron maths, observing log lines, one alert per missed run. Offline."""
import json
from datetime import datetime, timedelta

import pytest

from sre_agent.core.db import list_items
from sre_agent.sre import issues, schedules

MONDAY_9 = datetime(2026, 11, 9, 9, 0).astimezone()  # a Monday
JOB = {"name": "weekly-timesheet", "service": "timesheet", "cron": "0 9 * * 1",
       "match": "timesheet reminder job started", "grace_minutes": 15}


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    f = tmp_path / "jobs.json"
    f.write_text(json.dumps({"jobs": [JOB]}))
    monkeypatch.setenv("SRE_JOBS_FILE", str(f))
    monkeypatch.setattr("sre_agent.sre.schedules.notify", lambda *a, **k: None)
    return f


def test_cron_last_due():
    c = schedules.parse_cron("0 9 * * 1")
    assert schedules.last_due(c, MONDAY_9 + timedelta(hours=1)) == MONDAY_9
    assert schedules.last_due(c, MONDAY_9 + timedelta(days=3)) == MONDAY_9
    assert schedules.last_due(c, MONDAY_9 - timedelta(minutes=1)) == MONDAY_9 - timedelta(days=7)
    every15 = schedules.parse_cron("*/15 * * * *")
    assert schedules.last_due(every15, datetime(2026, 11, 9, 9, 40).astimezone()) == \
        datetime(2026, 11, 9, 9, 30).astimezone()
    assert schedules.last_due(schedules.parse_cron("0 8 1 * *"), MONDAY_9) == datetime(2026, 11, 1, 8, 0).astimezone()


@pytest.mark.parametrize("bad", ["", "* * * *", "61 * * * *", "* 25 * * *", "a b c d e", "*/0 * * * *"])
def test_bad_cron_rejected(bad):
    with pytest.raises(ValueError):
        schedules.parse_cron(bad)


def test_bad_job_is_skipped_not_fatal(tmp_path, monkeypatch):
    f = tmp_path / "jobs.json"
    f.write_text(json.dumps([JOB, {"name": "broken", "cron": "nope", "match": "x"}, {"cron": "0 9 * * 1"}]))
    monkeypatch.setenv("SRE_JOBS_FILE", str(f))
    assert [j.name for j in schedules.load_jobs()] == ["weekly-timesheet"]


def _arm():
    """The agent has been watching since before the run was due."""
    schedules.observe([])
    schedules.check_missed(now=MONDAY_9 - timedelta(days=1))


def test_missed_job_files_one_signal_once(jobs):
    _arm()
    assert schedules.check_missed(now=MONDAY_9 + timedelta(minutes=10)) == []  # still inside the grace period
    filed = schedules.check_missed(now=MONDAY_9 + timedelta(minutes=30))
    assert len(filed) == 1
    sig = next(i for i in list_items(kind="signal") if i["id"] == filed[0])["data"]
    assert sig["category"] == "job_missed" and sig["details"]["job"] == "weekly-timesheet"
    assert schedules.check_missed(now=MONDAY_9 + timedelta(hours=2)) == []  # same run: no second alert


def test_job_that_ran_is_not_reported(jobs):
    _arm()
    schedules.observe(["2026-11-09T09:00:03 [INFO] [timesheet] timesheet reminder job started"],
                      now=MONDAY_9 + timedelta(minutes=1))
    assert schedules.check_missed(now=MONDAY_9 + timedelta(hours=1)) == []


def test_no_alert_when_agent_was_not_watching_at_due_time(jobs):
    schedules.observe([])
    assert schedules.check_missed(now=MONDAY_9 + timedelta(hours=1)) == []  # first look is after the due time


def test_silent_monitor_means_no_verdict(jobs, monkeypatch):
    _arm()
    monkeypatch.setattr(schedules, "_last_poll", 0.0)
    assert schedules.check_missed(now=MONDAY_9 + timedelta(hours=1)) == []


def test_recovery_closes_the_issue_so_next_miss_gets_a_card(jobs):
    _arm()
    first = schedules.check_missed(now=MONDAY_9 + timedelta(minutes=30))[0]
    issue_id = next(i for i in list_items(kind="signal") if i["id"] == first)["data"]["issue_id"]
    next_monday = MONDAY_9 + timedelta(days=7)
    schedules.observe([f"{next_monday:%Y-%m-%dT%H:%M:%S} timesheet reminder job started"],
                      now=next_monday + timedelta(minutes=1))
    schedules.check_missed(now=next_monday + timedelta(hours=1))
    assert issues.get_issue(issue_id)["data"]["state"] == "fixed"
    third_monday = next_monday + timedelta(days=7)
    assert len(schedules.check_missed(now=third_monday + timedelta(minutes=30))) == 1  # regression: new card


def test_regex_match_and_node_log_timestamps(jobs, tmp_path, monkeypatch):
    f = tmp_path / "jobs2.json"
    f.write_text(json.dumps([{**JOB, "match": r"reminder job (started|begin)", "regex": True}]))
    monkeypatch.setenv("SRE_JOBS_FILE", str(f))
    _arm()
    schedules.observe(["2026-11-09 09:00:04:184 [info] timesheet Reminder Job begin"],  # node logger format
                      now=MONDAY_9 + timedelta(minutes=1))
    assert schedules.check_missed(now=MONDAY_9 + timedelta(hours=1)) == []

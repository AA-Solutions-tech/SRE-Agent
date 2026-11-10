"""Scheduled jobs and quiet hours read in an explicit timezone, so a UTC app on an IST server is not "missed". Offline."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.sre import schedules, severity

UTC = timezone.utc
IST = schedules.zone("Asia/Kolkata")
MONDAY_9_IST = datetime(2026, 11, 9, 3, 30, tzinfo=UTC)  # Monday 09:00 in Asia/Kolkata (UTC+5:30)
JOB = {"name": "weekly-timesheet", "service": "timesheet", "cron": "0 9 * * 1", "timezone": "Asia/Kolkata",
       "match": "timesheet reminder job started", "grace_minutes": 15}


def _jobs(tmp_path, monkeypatch, **overrides):
    f = tmp_path / "jobs.json"
    f.write_text(json.dumps({"jobs": [{**JOB, **overrides}]}))
    monkeypatch.setenv("SRE_JOBS_FILE", str(f))
    monkeypatch.setattr("sre_agent.sre.schedules.notify", lambda *a, **k: None)


def _armed():
    schedules.observe([], now=MONDAY_9_IST - timedelta(days=1))
    schedules.check_missed(now=MONDAY_9_IST - timedelta(days=1))


def test_the_schedule_is_read_in_the_jobs_own_timezone():
    cron = schedules.parse_cron("0 9 * * 1")
    due = schedules.last_due(cron, MONDAY_9_IST + timedelta(hours=1), tz=IST)
    assert due == MONDAY_9_IST and due.utcoffset() == timedelta(hours=5, minutes=30)
    assert schedules.last_due(cron, MONDAY_9_IST + timedelta(hours=1), tz=schedules.zone("UTC")) == datetime(2026, 11, 9, 9, 0, tzinfo=UTC) - timedelta(days=7)


def test_daylight_saving_moves_the_due_time_in_utc():
    cron, ny = schedules.parse_cron("0 9 * * 1"), schedules.zone("America/New_York")
    winter = schedules.last_due(cron, datetime(2026, 11, 9, 20, 0, tzinfo=UTC), tz=ny)
    summer = schedules.last_due(cron, datetime(2026, 7, 6, 20, 0, tzinfo=UTC), tz=ny)
    assert winter.astimezone(UTC).hour == 14 and summer.astimezone(UTC).hour == 13


def test_an_unknown_timezone_is_an_error_not_a_silent_default(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        schedules.zone("Mars/Olympus")
    _jobs(tmp_path, monkeypatch, timezone="Mars/Olympus")
    assert schedules.load_jobs() == []  # the bad job is skipped (and logged), the others keep working


def test_a_utc_app_on_an_ist_schedule_is_not_reported_missed(tmp_path, monkeypatch):
    _jobs(tmp_path, monkeypatch, log_timezone="UTC")
    _armed()
    line = "[2026-11-09 03:31:00] [INFO] [timesheet] timesheet reminder job started"  # 03:31 UTC = 09:01 IST
    schedules.observe([line], now=MONDAY_9_IST + timedelta(minutes=2))
    assert schedules.check_missed(now=MONDAY_9_IST + timedelta(hours=1)) == []


def test_the_same_line_read_in_the_wrong_zone_does_look_missed(tmp_path, monkeypatch):
    """Proves the setting matters: read as IST, 03:31 is the night before, so the Monday run looks absent."""
    _jobs(tmp_path, monkeypatch, log_timezone="Asia/Kolkata")
    _armed()
    line = "[2026-11-09 03:31:00] [INFO] [timesheet] timesheet reminder job started"
    schedules.observe([line], now=MONDAY_9_IST + timedelta(minutes=2))
    assert len(schedules.check_missed(now=MONDAY_9_IST + timedelta(hours=1))) == 1


def test_timezones_can_come_from_the_environment(tmp_path, monkeypatch):
    _jobs(tmp_path, monkeypatch, timezone="", log_timezone="")
    monkeypatch.setenv("JOBS_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setenv("LOG_TIMEZONE", "UTC")
    job = schedules.load_jobs()[0]
    assert str(job.tz) == "Asia/Kolkata" and str(job.log_tz) == "UTC"


def test_lines_with_an_explicit_offset_ignore_the_log_timezone():
    now = datetime(2026, 11, 9, 5, 0, tzinfo=UTC)
    t = schedules._line_time("[2026-11-09T03:31:00Z] [INFO] [x] hi", now, tz=schedules.zone("Asia/Tokyo"))
    assert t == datetime(2026, 11, 9, 3, 31, tzinfo=UTC)


def test_quiet_hours_are_read_in_the_configured_zone(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS", "22:00-07:00")
    evening_utc = datetime(2026, 1, 1, 20, 0, tzinfo=UTC)  # 01:30 the next morning in IST
    assert severity.in_quiet_hours(evening_utc) is False  # no zone set: read as given (20:00)
    monkeypatch.setenv("QUIET_TIMEZONE", "Asia/Kolkata")
    assert severity.in_quiet_hours(evening_utc) is True
    assert severity.in_quiet_hours(datetime(2026, 1, 1, 8, 0, tzinfo=UTC)) is False  # 13:30 IST
    monkeypatch.setenv("QUIET_TIMEZONE", "Not/AZone")
    assert severity.in_quiet_hours(datetime(2026, 1, 1, 23, 0)) is True  # unknown zone: falls back to the given time

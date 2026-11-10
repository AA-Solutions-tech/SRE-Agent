"""End to end: monitor -> incident for operational errors (config, bad recipient, rate limit, server).

They must produce an "action needed" notice (no Approve/Reject, no coding agent), say what was checked,
and repeats of the same problem must not produce new cards. Offline.
"""
import socket

import pytest

from sre_agent.core.approvals import handle_decision
from sre_agent.core.db import get_item, list_items
from sre_agent.core.notify import approval_token
from sre_agent.sre import issues
from sre_agent.sre.incident import agent as incident
from sre_agent.sre.monitor import agent as monitor

TS = "[2026-10-09T11:00:01Z]"


@pytest.fixture
def cards(monkeypatch):
    calls = []
    monkeypatch.setattr("sre_agent.core.approvals.notify", lambda t, b, buttons=None: calls.append((t, b, buttons)))
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: None)
    return calls


def _append(path, *lines):
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _run_monitor(path):
    return monitor.run(source=str(path), mode="once")


def _signal_id(out):
    assert out.startswith("signal "), out
    return out.split()[1].rstrip(":")


# ---- case 1: key missing from the app's env ----


def test_missing_env_key_gets_a_notice_with_the_real_cause_and_no_approve_button(tmp_path, monkeypatch, cards):
    env = tmp_path / "app.env"
    env.write_text("SMTP_USER=a\nSMTP_PASSWORD=\n")
    monkeypatch.setenv("APP_ENV_FILES", str(env))
    log = tmp_path / "app.log"
    _append(log, f"{TS} [ERROR] [timesheet] Missing required env var SMTP_PASSWORD")
    sid = _signal_id(_run_monitor(log))
    assert get_item(sid)["data"]["category"] == "config"

    incident.run(signal_id=sid)
    title, body, buttons = cards[0]
    assert title.startswith("action needed: timesheet HIGH")
    assert [b[0] for b in buttons] == ["I'll fix this", "Ignore", "Ignore similar"]
    assert "`SMTP_PASSWORD` in" in body and ": empty" in body  # verified against the env file, not guessed
    assert "no code fix to approve" in body and "WHAT WAS CHECKED" in body
    assert "sk-" not in body

    inv = list_items(kind="investigation")[0]["data"]
    assert inv["notice"] is True and inv["fix_type"] == "ops" and inv["affected_files"] == []


def test_missing_key_without_an_env_file_says_it_could_not_verify(tmp_path, monkeypatch, cards):
    monkeypatch.delenv("APP_ENV_FILES", raising=False)
    log = tmp_path / "app.log"
    _append(log, f"{TS} [ERROR] [api] Missing required env var STRIPE_SECRET_KEY")
    incident.run(signal_id=_signal_id(_run_monitor(log)))
    body = cards[0][1]
    assert "Not verified" in body and "APP_ENV_FILES" in body and "log line only" in body


def test_i_will_fix_this_on_a_notice_starts_reminders_not_coding(tmp_path, monkeypatch, cards):
    monkeypatch.delenv("APP_ENV_FILES", raising=False)
    log = tmp_path / "app.log"
    _append(log, f"{TS} [ERROR] [api] Missing required env var STRIPE_SECRET_KEY")
    incident.run(signal_id=_signal_id(_run_monitor(log)))
    inv_id = list_items(kind="investigation")[0]["id"]
    msg = handle_decision(inv_id, incident.STAGE, "developer", approval_token(inv_id, incident.STAGE))
    assert "coding agent will not run" in msg
    assert issues.find_by_fingerprint(issues.fingerprint("api", "Missing required env var STRIPE_SECRET_KEY"))[
        "data"]["state"] == "developer_fix"


# ---- case 2: scheduled email fails for one dummy recipient ----


def test_bad_recipient_is_a_data_notice_and_other_recipients_do_not_make_new_cards(tmp_path, cards):
    log = tmp_path / "app.log"
    _append(log, f"{TS} [ERROR] [timesheet] Failed to send timesheet reminder to dummy@example.com: "
                 "550 5.1.1 user unknown")
    sid = _signal_id(_run_monitor(log))
    assert get_item(sid)["data"]["category"] == "data"
    incident.run(signal_id=sid)
    title, body, buttons = cards[0]
    assert "dummy@example.com" in body and "not a code problem" in body
    assert "job itself ran" in body and len(buttons) == 3

    _append(log, f"{TS} [ERROR] [timesheet] Failed to send timesheet reminder to other@corp.io: "
                 "550 5.1.1 user unknown")
    out = _run_monitor(log)
    assert out.startswith("no new signals") and "known issue" in out  # same problem, different address
    assert len(list_items(kind="issue")) == 1


# ---- rate limits: watched, only a card when they persist ----


def test_rate_limit_is_counted_quietly_then_escalates(tmp_path, monkeypatch, cards):
    monkeypatch.setenv("WATCH_THRESHOLD", "3")
    log = tmp_path / "app.log"
    line = f"{TS} [WARN] [api] Request failed with status code 429 Too Many Requests"
    for _ in range(2):
        _append(log, line)
        assert _run_monitor(log).startswith("no new signals")
    assert issues.find_by_fingerprint(issues.fingerprint("api", "Request failed with status code 429 Too Many Requests"))[
        "data"]["state"] == "watching"
    _append(log, line)
    sid = _signal_id(_run_monitor(log))  # third hit inside the window: now it is a card
    incident.run(signal_id=sid)
    title, body, buttons = cards[0]
    assert "Rate limit" in body and "Persisted past the alert threshold" in body and len(buttons) == 3


def test_one_burst_in_a_single_poll_counts_every_line(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_THRESHOLD", "3")
    log = tmp_path / "app.log"
    _append(log, *[f"{TS} [WARN] [api] rate limit exceeded for key {i}" for i in range(4)])
    assert _run_monitor(log).startswith("signal ")


def test_watching_issue_that_goes_quiet_is_closed_silently(tmp_path):
    from datetime import datetime, timedelta, timezone

    log = tmp_path / "app.log"
    _append(log, f"{TS} [WARN] [api] rate limit exceeded")
    _run_monitor(log)
    assert issues.sweep(now=datetime.now(timezone.utc) + timedelta(hours=2)) == []
    assert issues.list_items(kind="issue")[0]["data"]["state"] == "fixed"


# ---- server: unreachable dependency is checked, not guessed ----


def test_unreachable_local_port_is_reported_by_a_real_check(tmp_path, cards):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again on exit: nothing listens here
    log = tmp_path / "app.log"
    _append(log, f"{TS} [ERROR] [api] connect ECONNREFUSED 127.0.0.1:{port}")
    incident.run(signal_id=_signal_id(_run_monitor(log)))
    body = cards[0][1]
    assert f"`127.0.0.1:{port}` is NOT reachable" in body and "Server or infrastructure" in body


# ---- code bugs keep the old flow ----


def test_unclassified_error_still_gets_the_approve_reject_card(tmp_path, cards):
    log = tmp_path / "app.log"
    _append(log, f"{TS} [ERROR] [api] TypeError: Cannot read properties of undefined (reading 'name')")
    incident.run(signal_id=_signal_id(_run_monitor(log)))
    assert [b[0] for b in cards[0][2]] == ["Approve", "Reject", "I'll fix this", "Ignore", "Ignore similar"]

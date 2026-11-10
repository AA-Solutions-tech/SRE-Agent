"""Pilot gaps 7-12: Slack decisions, several sources, delivery proof, quiet idle polls, start at the end, readable logs,
/stats. Offline."""
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from sre_agent import doctor, server
from sre_agent.core import approvals
from sre_agent.core import notify as n
from sre_agent.core.db import create_item, get_item, list_items, update_item
from sre_agent.sre import issues, slack_app
from sre_agent.sre.incident import agent as incident
from sre_agent.sre.monitor import agent as monitor
from sre_agent.sre.monitor import parse
from sre_agent.sre.monitor.sources import hint_for, is_file_source, parse_sources, readable_problem, split_source
from sre_agent.wizard import steps
from sre_agent.wizard import validators as v
from test_wizard import Script  # shared scripted-answer helper

TS = "[2026-10-09T11:00:01Z]"
SECRET = "slack-signing-secret"
client = TestClient(server.app)
decisions = []


@approvals.on_approval("slacktest", extra_actions={"ignore": "Ignore"})
def _handler(item_id, action):
    decisions.append((item_id, action))
    return f"did {action}"


# ---------------- gap 7: Slack buttons decide inside Slack ----------------


@pytest.fixture
def slack(monkeypatch, cfg):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)
    monkeypatch.setenv("APPROVAL_ACCEPT_LEGACY", "false")
    cfg(approval_secret="a-long-test-secret-0123456789", app_base_url="http://localhost:8000")
    decisions.clear()
    posted = []
    monkeypatch.setattr(slack_app.httpx, "post", lambda url, json=None, timeout=None, **k: posted.append((url, json)))
    return posted


@pytest.fixture
def card():
    item = create_item("slacktest", {"service": "api", "error": "TypeError: boom"}, status="awaiting_slacktest")
    return item, {b[0]: b for b in approvals.fresh_buttons(item, "slacktest")}


def _press(button, user=None, **extra):
    payload = {"type": "block_actions", "user": user or {"id": "U1", "username": "chaitanya"},
               "response_url": "https://hooks.slack.test/r",
               "message": {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "card"}},
                                      {"type": "actions", "elements": []}]},
               "actions": [{"action_id": button[3].split("|", 1)[0], "value": button[3], **extra}]}
    raw = urlencode({"payload": json.dumps(payload)}).encode()
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(SECRET.encode(), b"v0:" + ts.encode() + b":" + raw, hashlib.sha256).hexdigest()
    return client.post("/slack/interactions", content=raw, headers={
        "content-type": "application/x-www-form-urlencoded", "x-slack-request-timestamp": ts, "x-slack-signature": sig})


def test_buttons_carry_a_signed_value_so_slack_can_decide_without_a_web_page(card):
    item, buttons = card
    approve = buttons["Approve"]
    assert approve[3].startswith(f"decide:approve|{item}|slacktest|") and "http://" in approve[1]
    assert {b[3].split("|", 1)[0] for b in buttons.values()} == {"decide:approve", "decide:reject", "decide:ignore"}


def test_with_the_slack_app_the_card_has_real_buttons_otherwise_links(slack, card, monkeypatch):
    _, buttons = card
    els = n._slack_blocks("t", "b", list(buttons.values()))[-1]["elements"]
    assert [e["action_id"] for e in els] == ["decide:approve", "decide:reject", "decide:ignore"]
    assert all("url" not in e for e in els)
    monkeypatch.delenv("SLACK_BOT_TOKEN")
    els = n._slack_blocks("t", "b", list(buttons.values()))[-1]["elements"]
    assert all("url" in e and "action_id" not in e for e in els)  # plain links to the confirmation page


def test_pressing_approve_in_slack_decides_and_records_who(slack, card):
    item, buttons = card
    assert _press(buttons["Approve"]).status_code == 200
    assert decisions == [(item, "approve")] and get_item(item)["status"] == "slacktest_approved"
    who = get_item(item)["data"]["decided_by"]
    assert who["slack_user"] == "U1" and who["name"] == "chaitanya" and who["via"] == "slack"
    url, body = slack[-1]
    assert url == "https://hooks.slack.test/r" and body["replace_original"] is True
    assert "<@U1>" in body["text"] and not any(b["type"] == "actions" for b in body["blocks"])  # buttons are gone


def test_a_second_press_is_refused_politely(slack, card):
    item, buttons = card
    _press(buttons["Approve"])
    _press(buttons["Reject"])
    assert decisions == [(item, "approve")]
    assert "already handled" in slack[-1][1]["text"] and slack[-1][1]["response_type"] == "ephemeral"


def test_only_allowed_slack_users_can_decide(slack, card, monkeypatch):
    item, buttons = card
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U9, U8")
    _press(buttons["Approve"])
    assert not decisions and get_item(item)["status"] == "awaiting_slacktest"
    assert "not on the list" in slack[-1][1]["text"]
    _press(buttons["Approve"], user={"id": "U9", "username": "boss"})
    assert decisions == [(item, "approve")]


def test_a_tampered_or_expired_button_does_nothing(slack, card):
    item, buttons = card
    tampered = list(buttons["Reject"])
    tampered[3] = tampered[3].replace("decide:reject", "decide:approve")  # reject's token, approve's name
    _press(tampered)
    assert not decisions and "no longer valid" in slack[-1][1]["text"]
    stale = list(buttons["Approve"])
    stale[3] = f"decide:approve|{item}|slacktest|1|{n.approval_token(item, 'slacktest', 'approve', 1)}"  # expired in 1970
    _press(stale)
    assert not decisions and "expired" in slack[-1][1]["text"]
    assert get_item(item)["status"] == "awaiting_slacktest"


def test_an_unknown_item_is_reported_not_crashed_on(slack):
    ghost = ("Approve", "u", "primary", f"decide:approve|ghost|slacktest|{int(time.time()) + 99}|"
             + n.approval_token("ghost", "slacktest", "approve", int(time.time()) + 99))
    assert _press(ghost).status_code == 200 and "no longer exists" in slack[-1][1]["text"]


def test_the_doctor_flags_a_loopback_base_url(cfg, monkeypatch):
    for key in ("SLACK_SIGNING_SECRET", "SLACK_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    cfg(app_base_url="http://localhost:8000", notify_channels=("slack",))
    status, name, detail = doctor.check_app_base_url()
    assert (status, name) == ("WARN", "APP_BASE_URL") and "loopback" in detail and "Interactivity" in detail
    cfg(app_base_url="http://127.0.0.1:8000")
    assert doctor.check_app_base_url()[0] == "WARN"
    cfg(app_base_url="https://sre.example.com")
    assert doctor.check_app_base_url()[0] == "OK"
    cfg(app_base_url="http://sre.example.com")
    assert doctor.check_app_base_url()[0] == "WARN" and "plain http" in doctor.check_app_base_url()[2]


def test_loopback_is_fine_when_slack_decides_and_it_is_the_only_channel(cfg, monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    cfg(app_base_url="http://localhost:8000", notify_channels=("slack",))
    assert doctor.check_app_base_url()[0] == "OK"
    cfg(notify_channels=("slack", "email"))  # email links would still be dead
    assert doctor.check_app_base_url()[0] == "WARN"


def test_the_wizard_warns_about_a_loopback_address_and_stays_quiet_otherwise():
    s = Script("http://localhost:8000", "", "")
    steps.web(s.prompter(), {})
    assert "WARNING: this is a loopback address" in s.printed()
    s = Script("https://sre.example.com", "", "")
    steps.web(s.prompter(), {})
    assert "loopback" not in s.printed()
    assert v.is_loopback_url("http://127.0.0.1:1") and not v.is_loopback_url("https://sre.example.com")


# ---------------- gap 8: several sources, each with its own service name ----------------


@pytest.mark.parametrize("raw,expected", [
    ("/var/log/qa.log@qa-api", ("/var/log/qa.log", "qa-api")),
    ("/var/log/app.log", ("/var/log/app.log", None)),
    ("docker:web@storefront", ("docker:web", "storefront")),
    ("journald:app.service@billing", ("journald:app.service", "billing")),
    ("/home/me@corp/app.log", ("/home/me@corp/app.log", None)),   # an @ inside a path is just part of the path
    ("ssh", ("ssh", None)), ("", ("", None)),
])
def test_a_source_may_name_its_service(raw, expected):
    assert split_source(raw) == expected


def test_a_list_of_sources_is_parsed():
    assert parse_sources("/a.log@qa-api, /b.log@prod-api ,docker:web") == [("/a.log", "qa-api"), ("/b.log", "prod-api"), ("docker:web", None)]
    assert is_file_source("/a.log") and not is_file_source("docker:web") and not is_file_source("ssh")


def test_cards_from_each_source_carry_that_sources_service(tmp_path, monkeypatch):
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: None)
    line = json.dumps({"level": "error", "message": "Payment webhook failed", "service": "url-shortener", "timestamp": "t"})
    qa, prod = tmp_path / "qa.log", tmp_path / "prod.log"
    qa.write_text(line + "\n")
    prod.write_text(line + "\n")
    monitor.run(source=f"{qa}@qa-api", mode="once")
    monitor.run(source=f"{prod}@prod-api", mode="once")
    assert sorted(i["data"]["service"] for i in list_items(kind="signal")) == ["prod-api", "qa-api"]
    assert len(list_items(kind="issue")) == 2  # the same message on two services is two issues, as it should be


def test_without_a_name_the_log_lines_own_service_is_kept():
    parts = parse.classify([json.dumps({"level": "error", "message": "x failed", "service": "api"})])
    assert parts["signal"][0]["service"] == "api"
    assert parse.classify([json.dumps({"level": "error", "message": "x failed", "service": "api"})], "prod-api")["signal"][0]["service"] == "prod-api"


@pytest.mark.parametrize("value,ok", [
    ("/var/log/a.log", True), ("/a.log@qa-api, /b.log@prod-api", True), ("docker:web@storefront, journald:app.service", True),
    ("C:\\logs\\a.log", True), ("relative/a.log", False), ("", False), ("docker:", False), ("/a.log, rel.log", False),
])
def test_the_wizard_accepts_a_list_of_sources(value, ok):
    assert (v.log_sources(value) == "") is ok


def test_the_wizard_step_stores_the_list_as_typed():
    s = Script("2", "shop", "/var/log/qa.log@qa-api, /var/log/prod.log@prod-api")
    out = steps.logs(s.prompter(), {})
    assert out["MONITOR_LOG_PATH"] == "/var/log/qa.log@qa-api, /var/log/prod.log@prod-api"
    assert "separate with commas" in s.printed() and "/home" in s.printed()


# ---------------- gap 9: delivery proof and quiet idle polls ----------------


@pytest.fixture
def channels(monkeypatch, cfg):
    cfg(notify_channels=("slack", "email"))
    alerts = []
    monkeypatch.setattr("sre_agent.sre.health.alert", lambda key, title, detail, **kw: alerts.append((key, detail)) or True)
    return alerts


def test_every_channel_result_is_recorded(channels, monkeypatch):
    monkeypatch.setattr(n, "send_slack", lambda *a, **k: 200)
    monkeypatch.setattr(n, "send_email", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("smtp down")))
    out = n.notify("t", "b")
    assert [(d["channel"], d["ok"], d["status"]) for d in out] == [("slack", True, 200), ("email", False, None)]
    assert "smtp down" in out[1]["error"] and out[0]["at"]
    assert not channels  # one channel got it: nobody is in the dark


def test_an_http_error_keeps_its_status_code(channels, monkeypatch):
    def refuse(*a, **k):
        raise httpx.HTTPStatusError("forbidden", request=httpx.Request("POST", "http://x"), response=httpx.Response(403))

    monkeypatch.setattr(n, "send_slack", refuse)
    monkeypatch.setattr(n, "send_email", lambda *a, **k: True)
    assert n.notify("t", "b")[0]["status"] == 403


def test_when_every_channel_fails_it_is_reported(channels, monkeypatch):
    monkeypatch.setattr(n, "send_slack", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("webhook revoked")))
    monkeypatch.setattr(n, "send_email", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("smtp down")))
    n.notify("t", "b")
    assert channels[0][0] == "notify_delivery" and "webhook revoked" in channels[0][1] and "smtp down" in channels[0][1]


def test_unconfigured_channels_are_not_failures(channels, monkeypatch):
    monkeypatch.setattr(n, "send_slack", lambda *a, **k: None)   # prints instead: nothing configured
    monkeypatch.setattr(n, "send_email", lambda *a, **k: None)
    out = n.notify("t", "b")
    assert [d["ok"] for d in out] == [None, None] and not channels


def test_a_card_records_where_it_was_delivered(monkeypatch):
    result = [{"channel": "slack", "ok": True, "status": 200, "at": "now"}]
    monkeypatch.setattr("sre_agent.core.approvals.notify", lambda t, b, buttons=None, **kw: result)
    item = create_item("slacktest", {})
    assert approvals.request_approval(item, "slacktest", "t", "b") == result
    assert get_item(item)["data"]["delivery"] == result


def test_delivery_reaches_the_issue_and_the_api(tmp_path, monkeypatch):
    result = [{"channel": "slack", "ok": True, "status": 200, "at": "now"}]
    monkeypatch.setattr("sre_agent.core.approvals.notify", lambda t, b, buttons=None, **kw: result)
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: result)
    log = tmp_path / "app.log"
    log.write_text(f"{TS} [ERROR] [api] TypeError: boom in handler\n")
    sid = monitor.run(source=str(log), mode="once").split()[1].rstrip(":")
    assert get_item(sid)["data"]["delivery"] == result            # the "signal detected" message
    incident.run(signal_id=sid)
    issue = list_items(kind="issue")[0]
    assert issue["data"]["last_delivery"] == result               # the card
    assert client.get(f"/issues/{issue['id']}").json()["last_delivery"] == result


def test_debug_lines_are_not_printed_to_the_console(monkeypatch, capsys):
    from sre_agent.core.sre_log import get_logger, step

    monkeypatch.setenv("SRE_TRACE", "true")
    log = get_logger("t")
    step(log, "t.01", "idle poll", level="debug")
    step(log, "t.02", "something happened")
    out = capsys.readouterr().out
    assert "idle poll" not in out and "something happened" in out
    monkeypatch.setenv("SRE_TRACE_DEBUG", "true")
    step(log, "t.03", "idle again", level="debug")
    assert "idle again" in capsys.readouterr().out


def test_an_idle_poll_logs_at_debug_and_a_busy_one_at_info(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("sre_agent.sre.monitor.agent.step",
                        lambda logger, step_id, msg, level="info", **f: seen.append((step_id, level)))
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: None)
    log = tmp_path / "app.log"
    log.write_text("")
    monitor.run(source=str(log), mode="once")
    idle = {s: lvl for s, lvl in seen}
    assert idle["monitor.04.collect"] == "debug" and idle["monitor.05.classify"] == "debug"
    seen.clear()
    log.write_text(f"{TS} [ERROR] [api] TypeError: boom in handler\n")
    monitor.run(source=str(log), mode="once")
    assert dict(seen)["monitor.04.collect"] == "info"


def test_the_watch_loops_idle_heartbeat_is_debug_too(tmp_path, monkeypatch):
    """The pilot's log filled with one INFO line per 5-second poll; an idle poll must be DEBUG end to end."""
    seen = []
    monkeypatch.setattr("sre_agent.sre.monitor.agent.step",
                        lambda logger, step_id, msg, level="info", **f: seen.append((step_id, level, f.get("result"))))
    log = tmp_path / "app.log"
    log.write_text("")
    polls = {"n": 0}

    def stop_after_two(_):
        polls["n"] += 1
        if polls["n"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(monitor.time, "sleep", stop_after_two)
    monitor.run(source=str(log), mode="watch", poll_seconds=1)
    heartbeats = [(lvl, res) for sid, lvl, res in seen if sid == "monitor.09.watch"]
    assert heartbeats and all(lvl == "debug" and res == "no new lines" for lvl, res in heartbeats)


# ---------------- gap 10: a live log is read from its end the first time ----------------


class _Graph:
    def invoke(self, state):
        raise KeyboardInterrupt


class _Reader:
    def close(self):
        pass


def test_watch_mode_starts_at_the_end_and_once_mode_still_reads_everything(monkeypatch, tmp_path):
    calls = []

    def fake_open(source, service=None, start_at_end=False):
        calls.append((source, service, start_at_end))
        return _Reader(), source, _Graph(), True

    monkeypatch.setattr(monitor, "_open_source", fake_open)
    monitor.run(source="/var/log/app.log@prod-api", mode="watch")
    assert calls[-1] == ("/var/log/app.log", "prod-api", True)
    monkeypatch.setenv("MONITOR_REPLAY_BACKLOG", "true")
    monitor.run(source="/var/log/app.log", mode="watch")
    assert calls[-1] == ("/var/log/app.log", None, False)
    monkeypatch.delenv("MONITOR_REPLAY_BACKLOG")
    monkeypatch.setattr(_Graph, "invoke", lambda self, state: {"summary": "done"})
    monitor.run(source="/var/log/app.log", mode="once")
    assert calls[-1][2] is False  # a one-off scan or a cron run is a snapshot: it reads the whole file


def test_a_new_live_log_reports_only_what_happens_next(tmp_path, monkeypatch):
    announced = []
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: announced.append(a))
    log = tmp_path / "app.log"
    log.write_text("".join(f"{TS} [ERROR] [api] Old failure number {i} happened\n" for i in range(8)))
    reader, label, graph, fresh = monitor._open_source(str(log), start_at_end=True)
    assert fresh is True and reader.offset == log.stat().st_size and monitor._load_offset(label) == reader.offset
    assert graph.invoke({"text": None, "source_name": label})["summary"] == "no new lines"
    with open(log, "a") as f:
        f.write(f"{TS} [ERROR] [api] TypeError: this one is new\n")
    out = graph.invoke({"text": None, "source_name": label})
    assert out["summary"].startswith("signal ") and len(list_items(kind="signal")) == 1
    _, _, _, fresh_again = monitor._open_source(str(log), start_at_end=True)
    assert fresh_again is False  # the saved position is used from now on


def test_replayed_history_is_marked_as_backlog(tmp_path, monkeypatch):
    titles = []
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda t, b, **k: titles.append(t))
    graph = monitor.build(None)
    text = f"{TS} [ERROR] [api] TypeError: old news\n"
    out = graph.invoke({"text": text, "source_name": "x", "backlog": True})
    sid = out["signal_id"]
    assert get_item(sid)["data"]["backlog"] is True and titles[0].endswith("(backlog)")
    titles.clear()
    graph.invoke({"text": text.replace("old news", "fresh news"), "source_name": "x", "backlog": False})
    assert not titles[0].endswith("(backlog)")


# ---------------- gap 11: say what is wrong with an unreadable log, and how to fix it ----------------


def test_a_readable_file_has_no_problem(tmp_path):
    f = tmp_path / "app.log"
    f.write_text("x")
    assert readable_problem(str(f)) is None


def test_a_log_under_home_gets_the_systemd_and_acl_commands():
    msg = readable_problem("/home/alice/app/logs/error.log")
    assert "ProtectHome=read-only" in msg and "setfacl -m u:sre-agent:x /home/alice" in msg
    assert "setfacl -R -m u:sre-agent:rX /home/alice/app/logs" in msg and "hides /home" in msg


def test_other_problems_are_named_plainly(tmp_path):
    assert "does not exist yet" in readable_problem(str(tmp_path / "later.log")) and "ProtectHome" not in readable_problem(str(tmp_path / "later.log"))
    assert "directory" in readable_problem(str(tmp_path))


def test_a_watcher_crash_carries_the_hint():
    assert "ProtectHome" in hint_for("/home/alice/e.log@api", FileNotFoundError("x"))
    assert "ProtectHome" in hint_for("/home/alice/e.log", PermissionError("x"))
    assert hint_for("/var/log/e.log", ValueError("x")) == "" and hint_for("docker:web", PermissionError("x")) == ""


def test_the_doctor_checks_every_log_file(tmp_path, monkeypatch):
    good = tmp_path / "good.log"
    good.write_text("x")
    monkeypatch.setenv("MONITOR_LOG_PATH", f"{good}@qa-api, docker:web@shop")
    status, name, detail = doctor.check_log_sources()
    assert (status, name) == ("OK", "log sources") and "1 file(s) readable" in detail
    monkeypatch.setenv("MONITOR_LOG_PATH", f"{good}, /home/alice/app/error.log@prod-api")
    status, _, detail = doctor.check_log_sources()
    assert status == "WARN" and "ProtectHome" in detail and "good.log" not in detail
    monkeypatch.setenv("MONITOR_LOG_PATH", "")
    assert doctor.check_log_sources()[0] == "WARN"
    monkeypatch.setenv("MONITOR_SOURCE", "ssh")
    assert doctor.check_log_sources()[0] == "OK"


def test_the_doctor_runs_the_new_checks_before_the_service_starts(monkeypatch):
    names = [r[1] for r in doctor.run_checks(quick=True)]
    assert "APP_BASE_URL" in names and "log sources" in names and "LLM call" not in names  # no model call at boot
    assert "LLM call" in [r[1] for r in doctor.run_checks(quick=False)]


def test_the_service_file_documents_the_home_directory_fix():
    unit = (Path(__file__).resolve().parent.parent / "packaging" / "linux" / "sre-agent.service").read_text()
    assert "ProtectHome=read-only" in unit and "setfacl" in unit


# ---------------- gap 12: a dashboard can tell "no data" from "on track" ----------------


def test_stats_say_no_data_until_someone_decides():
    body = client.get("/stats").json()
    assert body["decisions"] == 0 and body["decision_metrics"] == "no data"


def test_stats_count_decisions_analysis_quality_and_delivery():
    ok = [{"channel": "slack", "ok": True, "status": 200}]
    bad = [{"channel": "slack", "ok": False, "status": 403}]
    a = create_item("investigation", {"service": "api"}, status="x")
    create_item("investigation", {"service": "api", "ai_error": "BadRequestError"}, status="x")
    c = create_item("investigation", {"service": "api"}, status="x")
    update_item(a, decided_by={"slack_user": "U1", "via": "slack"})
    update_item(c, decided_from={"ip": "1.2.3.4"})
    i1, _ = issues.record_occurrence({"service": "s", "error": "one", "severity": "HIGH", "evidence": ["x"]}, "s1")
    i2, _ = issues.record_occurrence({"service": "s", "error": "two", "severity": "HIGH", "evidence": ["x"]}, "s2")
    issues.record_delivery(i1, ok)
    issues.record_delivery(i2, bad)
    body = client.get("/stats?days=7").json()
    assert body["investigations"] == 3 and body["ai_analysed"] == 2 and body["ai_template_fallback"] == 1
    assert body["decisions"] == 2 and body["decisions_via_slack"] == 1 and body["decision_metrics"] == "available"
    assert body["cards_delivered"] == 1 and body["cards_delivery_failed"] == 1 and body["issues"] == 2

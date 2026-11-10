"""Issue lifecycle: fingerprints, dedupe, regression, snooze, developer fix, reminders. Offline."""
from datetime import datetime, timedelta, timezone

from sre_agent.core.db import get_item
from sre_agent.sre import issues


def _sig(error="Failed to fetch account details: boom 17", service="wave-node"):
    return {"service": service, "severity": "HIGH", "error": error}


def test_fingerprint_ignores_ids_numbers_and_case():
    a = issues.fingerprint("s", "listThreads failed for 6a675ac4c282aba62409394a: invalid_grant 12")
    b = issues.fingerprint("s", "LISTTHREADS FAILED FOR 91bc34de56f71234aa09bb11: invalid_grant 99")
    assert a == b and a != issues.fingerprint("s", "something else") and a != issues.fingerprint("t", "x")


def test_repeat_is_counted_not_investigated_again():
    first, act1 = issues.record_occurrence(_sig(), "s1")
    again, act2 = issues.record_occurrence(_sig("Failed to fetch account details: boom 99"), "s2")
    assert (act1, act2) == ("investigate", "count") and first == again
    d = get_item(first)["data"]
    assert d["count"] == 2 and d["signal_ids"] == ["s1", "s2"] and d["state"] == "open"


def test_fixed_issue_reopens_as_regression():
    issue_id, _ = issues.record_occurrence(_sig(), "s1")
    issues.mark_fix_pending(issue_id)
    issues.mark_fixed(issue_id)
    _, action = issues.record_occurrence(_sig(), "s2")
    d = get_item(issue_id)["data"]
    assert action == "investigate" and d["state"] == "open" and d["regressions"] == 1


def test_rejected_issue_stays_quiet_until_snooze_ends(monkeypatch):
    issue_id, _ = issues.record_occurrence(_sig(), "s1")
    issues.mark_rejected(issue_id)
    assert issues.record_occurrence(_sig(), "s2")[1] == "count"
    from sre_agent.core.db import update_item

    update_item(issue_id, snooze_until=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    assert issues.record_occurrence(_sig(), "s3")[1] == "investigate"


def test_ignored_issue_returns_as_normal_card_after_threshold(monkeypatch):
    monkeypatch.setenv("ISSUE_IGNORE_THRESHOLD", "3")
    issue_id, _ = issues.record_occurrence(_sig(), "s1")
    issues.mark_ignored(issue_id)
    actions = [issues.record_occurrence(_sig(), f"r{i}")[1] for i in range(4)]
    assert actions == ["count", "count", "count", "investigate"]
    d = get_item(issue_id)["data"]
    assert d["state"] == "open" and d["investigation_id"] is None


def test_sweep_closes_quiet_issue_and_reminds_developer_fix_issue():
    quiet_id, _ = issues.record_occurrence(_sig("quiet error"), "q1")
    issues.mark_fix_pending(quiet_id)
    dev_id, _ = issues.record_occurrence(_sig("still failing error"), "d1")
    issues.mark_developer_fix(dev_id)
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    from sre_agent.core.db import update_item

    update_item(dev_id, last_seen=(later - timedelta(minutes=5)).isoformat())  # still failing at sweep time
    due = issues.sweep(now=later)
    assert get_item(quiet_id)["data"]["state"] == "fixed"
    assert [d["id"] for d in due] == [dev_id] and get_item(dev_id)["data"]["state"] == "developer_fix"
    assert issues.sweep(now=later) == []  # reminded once, not again until the interval passes


def test_reminder_email_names_state_count_and_finding(monkeypatch):
    from sre_agent.core.db import create_item
    from sre_agent.sre import auto

    sent = []
    monkeypatch.setattr(auto, "notify", lambda t, b, buttons=None: sent.append((t, b)))
    inv = create_item("investigation", {"summary": "token revoked"})
    issue_id, _ = issues.record_occurrence(_sig("reminder error"), "r1")
    issues.attach_investigation(issue_id, inv)
    issues.mark_developer_fix(issue_id)
    auto.send_reminder({**get_item(issue_id)["data"], "id": issue_id})
    title, body = sent[0]
    assert "still failing" in title and "developer chose" in body and "token revoked" in body and "seen 1" in body


def test_dated_log_path_template_expands_today():
    from sre_agent.sre.monitor.agent import expand_path

    assert expand_path("logs/error-{date}.log", datetime(2026, 10, 23)) == "logs/error-2026-10-23.log"
    assert expand_path("logs/plain.log") == "logs/plain.log"


def test_issues_endpoints_are_read_only_views():
    from fastapi.testclient import TestClient

    from sre_agent import server

    c = TestClient(server.app)
    a, _ = issues.record_occurrence(_sig("first error"), "s1")
    b, _ = issues.record_occurrence(_sig("second error", service="other"), "s2")
    issues.mark_developer_fix(b)
    listed = c.get("/issues").json()["issues"]
    assert {i["id"] for i in listed} == {a, b} and "signal_ids" not in listed[0]
    assert [i["id"] for i in c.get("/issues", params={"state": "developer_fix"}).json()["issues"]] == [b]
    assert [i["id"] for i in c.get("/issues", params={"service": "other"}).json()["issues"]] == [b]
    one = c.get(f"/issues/{a}").json()
    assert one["state"] == "open" and one["count"] == 1 and one["signal_ids"] == ["s1"]
    assert c.get("/issues/nope").status_code == 404
    assert c.post("/issues").status_code == 405  # nothing can be changed through this endpoint


def test_fingerprint_ignores_emails_urls_uuids_and_ips():
    a = issues.fingerprint("s", "mail failed for dummy@example.com via https://smtp.x.io/send?id=1 from 10.0.0.5")
    b = issues.fingerprint("s", "mail failed for other.person+tag@corp.co.uk via https://smtp.y.io/send?id=2 from 192.168.1.9")
    assert a == b
    u1 = issues.fingerprint("s", "job 550e8400-e29b-41d4-a716-446655440000 failed")
    u2 = issues.fingerprint("s", "job 123e4567-e89b-12d3-a456-426614174000 failed")
    assert u1 == u2 and a != u1


def test_transient_kind_waits_for_the_threshold_inside_the_window(monkeypatch):
    monkeypatch.setenv("WATCH_THRESHOLD", "3")
    sig = {**_sig("429 too many requests"), "category": "rate_limit", "evidence": ["x"]}
    issue_id, a1 = issues.record_occurrence(sig, "s1")
    assert a1 == "count" and get_item(issue_id)["data"]["state"] == "watching"
    assert issues.record_occurrence(sig, "s2")[1] == "count"
    assert issues.record_occurrence(sig, "s3")[1] == "investigate"
    assert get_item(issue_id)["data"]["state"] == "open"


def test_old_hits_fall_out_of_the_watch_window(monkeypatch):
    from sre_agent.core.db import update_item

    monkeypatch.setenv("WATCH_THRESHOLD", "2")
    sig = {**_sig("429 too many requests"), "category": "rate_limit", "evidence": ["x"]}
    issue_id, _ = issues.record_occurrence(sig, "s1")
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    update_item(issue_id, watch_events=[[old, 1]])
    assert issues.record_occurrence(sig, "s2")[1] == "count"  # the hour-old hit no longer counts
    assert issues.record_occurrence(sig, "s3")[1] == "investigate"


def test_code_errors_are_never_watched():
    assert issues.record_occurrence(_sig("TypeError: x"), "s1")[1] == "investigate"

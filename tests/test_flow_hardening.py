"""Queue instead of a blocked watcher, recovery of lost signals, investigation budget, database limits. Offline."""
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.core import db
from sre_agent.core.db import count_items, create_item, find_by_field, get_item, list_items, prune, update_item
from sre_agent.sre import auto, budget, issues, retention


@pytest.fixture(autouse=True)
def _clean_queue():
    auto.stop_workers()
    yield
    auto.stop_workers()


@pytest.fixture
def alerts(monkeypatch):  # noqa: D401
    out = []
    monkeypatch.setattr("sre_agent.sre.health.alert", lambda key, title, detail, **kw: out.append((key, title)) or True)
    return out


def _world(error="TypeError: boom", severity="HIGH", investigated=False, state=None, status="new"):
    """A stored signal linked to an issue, the way the monitor leaves them."""
    sig = {"service": "api", "error": error, "severity": severity, "evidence": ["x"]}
    sid = create_item("signal", sig, status=status)
    issue_id, _ = issues.record_occurrence(sig, sid)
    update_item(sid, issue_id=issue_id)
    if investigated:
        issues.attach_investigation(issue_id, "inv1")
    if state:
        update_item(issue_id, state=state)
    return sid, issue_id


def _age(item_id, minutes):
    """Pretend the record was created `minutes` ago."""
    with db._conn() as c:
        c.execute("UPDATE items SET created_at=? WHERE id=?",
                  ((datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(), item_id))


# ---------------- 3. the watcher only queues; workers do the slow work ----------------


def test_a_slow_investigation_does_not_block_the_watcher(monkeypatch):
    gate, done = threading.Event(), []

    def slow(signal_id):  # a real investigation: many model calls
        gate.wait(5)
        done.append(signal_id)

    monkeypatch.setattr(auto, "_try_handoff", lambda sid: slow(sid))
    sid, _ = _world()
    auto.start_workers(1)
    t0 = time.monotonic()
    auto._dispatch(sid)  # what the monitor's poll loop calls for each new signal
    assert time.monotonic() - t0 < 0.5 and not done  # returned at once while the worker is still busy
    gate.set()
    for _ in range(50):
        if get_item(sid)["status"] == "handed_off":
            break
        time.sleep(0.1)
    assert get_item(sid)["status"] == "handed_off" and done == [sid]


def test_without_workers_a_signal_is_handled_inline(monkeypatch):
    monkeypatch.setattr(auto, "_try_handoff", lambda sid: None)
    sid, _ = _world()
    auto._dispatch(sid)  # CLI / tests: no queue involved
    assert get_item(sid)["status"] == "handed_off" and not auto.workers_running()


def test_the_most_severe_signal_is_investigated_first():
    ids = {sev: _world(f"{sev} error", sev)[0] for sev in ("LOW", "CRITICAL", "MEDIUM", "HIGH")}
    for sid in ids.values():
        assert auto.enqueue_signal(sid)
    order = []
    while not auto._queue.empty():
        order.append(auto._queue.get_nowait()[2])
    assert order == [ids["CRITICAL"], ids["HIGH"], ids["MEDIUM"], ids["LOW"]]


def test_a_signal_is_never_queued_twice_at_once():
    sid, _ = _world()
    assert auto.enqueue_signal(sid) is True and auto.enqueue_signal(sid) is False
    assert auto._queue.qsize() == 1


def test_a_full_queue_postpones_signals_instead_of_dropping_them(monkeypatch):
    monkeypatch.setenv("HANDOFF_QUEUE_MAX", "2")
    first, second, third = (_world(f"e{i}")[0] for i in range(3))
    assert auto.enqueue_signal(first) and auto.enqueue_signal(second)
    assert auto.enqueue_signal(third) is False
    d = get_item(third)
    assert d["status"] == "handoff_failed" and "queue is full" in d["data"]["handoff_error"]
    assert d["data"]["handoff_attempts" if "handoff_attempts" in d["data"] else "handoff_error"]  # retried later, not given up


def test_a_worker_survives_a_signal_that_crashes(monkeypatch):
    seen = []

    def flaky(sid):
        seen.append(sid)
        if len(seen) == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(auto, "_on_signal", flaky)
    a, b = _world("a")[0], _world("b")[0]
    auto.start_workers(1)
    auto.enqueue_signal(a)
    auto.enqueue_signal(b)
    for _ in range(50):
        if len(seen) == 2:
            break
        time.sleep(0.1)
    assert seen == [a, b] and auto.workers_running()


def test_a_backlog_raises_one_alert_after_two_checks(monkeypatch):
    monkeypatch.setenv("HANDOFF_BACKLOG_ALERT", "2")
    sent = []
    monkeypatch.setattr("sre_agent.sre.health.notify", lambda t, b, buttons=None: sent.append(t))
    monkeypatch.setattr(auto, "workers_running", lambda: True)
    for i in range(3):
        auto.enqueue_signal(_world(f"e{i}")[0])
    auto._check_backlog()
    assert not sent  # one slow moment is not a backlog
    auto._check_backlog()
    auto._check_backlog()
    assert len(sent) == 1 and "backed up" in sent[0]


# ---------------- 4. a signal that was stored but never investigated is found again ----------------


def test_a_lost_signal_is_handed_off_again(monkeypatch):
    handled = []
    monkeypatch.setattr(auto, "_try_handoff", lambda sid: handled.append(sid))
    sid, _ = _world()  # stored, linked to an open issue, but the process died before the hand-off
    assert auto.reconcile_signals(min_age_minutes=0) == [sid]
    assert handled == [sid] and get_item(sid)["status"] == "handed_off"


def test_only_the_newest_signal_of_an_issue_is_handed_off(monkeypatch):
    handled = []
    monkeypatch.setattr(auto, "_try_handoff", lambda sid: handled.append(sid))
    older, issue_id = _world()
    newer = create_item("signal", {"service": "api", "error": "TypeError: boom", "severity": "HIGH", "evidence": ["x"]})
    update_item(newer, issue_id=issue_id)
    assert auto.reconcile_signals(min_age_minutes=0) == [newer] and handled == [newer]
    assert get_item(older)["status"] == "new"  # not handed off a second time; next pass settles it


def test_signals_that_need_nothing_are_marked_and_not_handed_off(monkeypatch):
    monkeypatch.setattr(auto, "_try_handoff", lambda sid: pytest.fail("must not hand off"))
    has_card, _ = _world("a", investigated=True)
    repeat, _ = _world("b", state="fixed")
    assert auto.reconcile_signals(min_age_minutes=0) == []
    assert get_item(has_card)["status"] == "handed_off" and get_item(repeat)["status"] == "counted"


def test_a_signal_never_linked_to_an_issue_is_linked_then_handled(monkeypatch):
    handled = []
    monkeypatch.setattr(auto, "_try_handoff", lambda sid: handled.append(sid))
    orphan = create_item("signal", {"service": "api", "error": "NewError: x", "severity": "HIGH", "evidence": ["x"]})
    assert auto.reconcile_signals(min_age_minutes=0) == [orphan]
    d = get_item(orphan)["data"]
    assert d["issue_id"] and handled == [orphan]


def test_a_signal_the_monitor_is_still_working_on_is_left_alone(monkeypatch):
    monkeypatch.setattr(auto, "_try_handoff", lambda sid: pytest.fail("too young to be lost"))
    _world()
    assert auto.reconcile_signals() == []  # default minimum age: a couple of minutes


def test_old_signals_outside_the_lookback_are_not_scanned(monkeypatch):
    monkeypatch.setattr(auto, "_try_handoff", lambda sid: pytest.fail("outside the window"))
    sid, _ = _world()
    _age(sid, 60 * 48)
    assert auto.reconcile_signals(min_age_minutes=0) == []


def test_held_signals_are_not_reconciled(monkeypatch):
    monkeypatch.setattr(auto, "_try_handoff", lambda sid: pytest.fail("quiet hours: held on purpose"))
    _world(status="held")
    assert auto.reconcile_signals(min_age_minutes=0) == []


# ---------------- 6. a flood of errors cannot become a flood of model calls ----------------


def test_the_hourly_budget_blocks_and_critical_gets_more_room(monkeypatch):
    monkeypatch.setenv("INVESTIGATIONS_PER_HOUR", "2")
    assert budget.allow("HIGH")
    budget.record("a")
    budget.record("b")
    assert not budget.allow("HIGH") and not budget.allow("LOW")
    assert budget.allow("CRITICAL")  # three times the limit
    for i in range(4):
        budget.record(f"c{i}")
    assert not budget.allow("CRITICAL")


def test_the_daily_budget_and_zero_means_unlimited(monkeypatch):
    monkeypatch.setenv("INVESTIGATIONS_PER_HOUR", "0")
    monkeypatch.setenv("INVESTIGATIONS_PER_DAY", "3")
    for i in range(3):
        budget.record(str(i))
    assert not budget.allow("HIGH")
    monkeypatch.setenv("INVESTIGATIONS_PER_DAY", "0")
    assert budget.allow("HIGH")


def test_old_investigations_stop_counting_against_the_hour(monkeypatch):
    monkeypatch.setenv("INVESTIGATIONS_PER_HOUR", "1")
    budget.record("old")
    old = list_items(kind=budget.KIND)[0]["id"]
    _age(old, 90)
    assert budget.allow("HIGH") and budget.usage() == (0, 1)


def test_over_budget_signals_wait_with_one_alert_and_are_released_later(monkeypatch, alerts):
    monkeypatch.setenv("INVESTIGATIONS_PER_HOUR", "1")
    monkeypatch.setattr(auto, "_try_handoff", lambda sid: None)
    first, _ = _world("one")
    second, _ = _world("two")
    auto._on_signal(first)
    auto._on_signal(second)
    assert get_item(first)["status"] == "handed_off" and get_item(second)["status"] == "throttled"
    assert [a[0] for a in alerts] == ["investigation_budget"]
    assert auto.reconcile_signals(min_age_minutes=0) == []  # still over the limit: keeps waiting
    monkeypatch.setenv("INVESTIGATIONS_PER_HOUR", "10")
    assert auto.reconcile_signals(min_age_minutes=0) == [second]
    assert get_item(second)["status"] == "handed_off"


# ---------------- database: indexed lookups, bounded scans, retention ----------------


def test_find_by_field_returns_the_newest_match_and_nothing_for_a_miss():
    create_item("issue", {"fingerprint": "abc", "n": 1})
    newest = create_item("issue", {"fingerprint": "abc", "n": 2})
    create_item("other", {"fingerprint": "abc"})
    assert find_by_field("issue", "fingerprint", "abc")["id"] == newest
    assert find_by_field("issue", "fingerprint", "zzz") is None
    with pytest.raises(ValueError):
        find_by_field("issue; DROP TABLE items", "fingerprint", "abc")
    with pytest.raises(ValueError):
        find_by_field("issue", "a') OR 1=1 --", "abc")


def test_find_by_field_falls_back_to_a_scan_without_json_support():
    item = create_item("issue", {"fingerprint": "abc"})
    real, calls = db._conn, {"n": 0}

    @contextmanager
    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("no such function: json_extract")
        with real() as c:
            yield c

    db_conn = db._conn
    db._conn = flaky
    try:
        assert find_by_field("issue", "fingerprint", "abc")["id"] == item
    finally:
        db._conn = db_conn


def test_the_indexes_exist():
    create_item("issue", {"fingerprint": "abc"})
    with db._conn() as c:
        names = {r[1] for r in c.execute("PRAGMA index_list(items)")}
    assert "idx_items_kind_status_created" in names and "idx_issue_fingerprint" in names


def test_list_items_can_be_bounded_by_time_and_count():
    old = create_item("signal", {"n": 0})
    _age(old, 60 * 48)
    new = [create_item("signal", {"n": i}) for i in range(1, 4)]
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    assert {i["id"] for i in list_items(kind="signal", since=since)} == set(new)
    assert len(list_items(kind="signal", limit=2)) == 2
    assert count_items("signal") == 4 and count_items("signal", since=since) == 3


def test_prune_removes_only_old_items_of_the_given_statuses():
    done_old = create_item("signal", {}, status="handed_off")
    waiting_old = create_item("signal", {}, status="new")
    done_new = create_item("signal", {}, status="handed_off")
    for i in (done_old, waiting_old):
        _age(i, 60 * 24 * 40)
    assert prune("signal", timedelta(days=30), ("handed_off", "counted")) == 1
    assert get_item(done_old) is None and get_item(waiting_old) and get_item(done_new)


def test_retention_keeps_everything_that_is_still_needed(monkeypatch):
    old = 60 * 24 * 400
    handled = create_item("signal", {}, status="handed_off")
    pending = [create_item("signal", {}, status=s) for s in ("new", "held", "throttled", "handoff_failed")]
    result = create_item("coding_result", {})
    marker = create_item(budget.KIND, {})
    closed_issue, _ = issues.record_occurrence({"service": "s", "error": "old closed", "severity": "HIGH", "evidence": ["x"]}, "s1")
    open_issue, _ = issues.record_occurrence({"service": "s", "error": "old but open", "severity": "HIGH", "evidence": ["x"]}, "s2")
    for i in (handled, *pending, result, marker, closed_issue, open_issue):
        _age(i, old)
    update_item(closed_issue, state="fixed", last_seen=(datetime.now(timezone.utc) - timedelta(days=400)).isoformat())
    update_item(open_issue, last_seen=(datetime.now(timezone.utc) - timedelta(days=400)).isoformat())
    removed = retention.run()
    assert removed == {"signals": 1, "results": 1, "budget": 1, "issues": 1}
    assert get_item(handled) is None and get_item(result) is None and get_item(marker) is None
    assert get_item(closed_issue) is None and get_item(open_issue) is not None
    assert all(get_item(i) is not None for i in pending)


def test_retention_rules_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("RETENTION_DAYS_SIGNALS", "0")
    sid = create_item("signal", {}, status="handed_off")
    _age(sid, 60 * 24 * 400)
    assert retention.run()["signals"] == 0 and get_item(sid)


def test_a_lookup_stays_fast_as_the_database_grows():
    rows = [(f"i{n:08d}", "issue", "new", '{"fingerprint": "%012x", "state": "open"}' % n, "t", "t") for n in range(20000)]
    with db._conn() as c:
        c.executemany("INSERT INTO items VALUES (?,?,?,?,?,?)", rows)
    t0 = time.perf_counter()
    for n in (5, 9999, 19999):
        assert find_by_field("issue", "fingerprint", "%012x" % n)["id"] == f"i{n:08d}"
    assert (time.perf_counter() - t0) / 3 < 0.05  # was about 0.2 s per lookup with 20,000 issues before the index

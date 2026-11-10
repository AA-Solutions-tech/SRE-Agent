"""Error spikes, new-error bursts, CRITICAL services, quiet hours and paging. Offline."""
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.core.db import get_item, list_items
from sre_agent.sre import anomaly, auto, severity
from sre_agent.sre.monitor import agent as monitor
from sre_agent.sre.supervisor import router_rules

T = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
TS = "[2026-10-09T11:00:01Z]"


@pytest.fixture
def alerts(monkeypatch):
    calls = []
    monkeypatch.setattr("sre_agent.sre.health.alert", lambda key, title, detail, **kw: calls.append((key, title, kw)) or True)
    anomaly.reset()
    yield calls
    anomaly.reset()


# ---- spikes / bursts ----


def test_spike_alerts_when_far_above_baseline(alerts):
    anomaly.record_errors(30, now=T)
    assert [a[0] for a in alerts] == ["error_spike"] and alerts[0][2]["page"] is True


def test_no_spike_for_small_or_normal_volume(alerts):
    anomaly.record_errors(10, now=T)  # under SPIKE_MIN_ERRORS
    anomaly.reset()
    anomaly._errors.append((T - timedelta(minutes=30), 600))  # busy baseline: 50 per 5 min
    anomaly.record_errors(30, now=T)
    assert not alerts


def test_burst_of_new_issues_but_not_when_spread_out(alerts):
    anomaly.record_new_issues(1, now=T)
    anomaly.record_new_issues(1, now=T + timedelta(minutes=1))
    assert not alerts
    anomaly.record_new_issues(1, now=T + timedelta(minutes=2))
    assert alerts[0][0] == "new_issue_burst"
    anomaly.reset()
    alerts.clear()
    for i in range(3):
        anomaly.record_new_issues(1, now=T + timedelta(minutes=30 * i))
    assert not alerts


def test_monitor_feeds_the_spike_detector(tmp_path, alerts, monkeypatch):
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: None)
    log = tmp_path / "app.log"
    log.write_text("".join(f"{TS} [ERROR] [api] TypeError: boom {i} in handler\n" for i in range(25)))
    monitor.run(source=str(log), mode="once")
    assert any(a[0] == "error_spike" for a in alerts)


# ---- critical services + routing ----


def test_critical_by_service_and_pattern(monkeypatch):
    monkeypatch.setenv("CRITICAL_SERVICES", "payment-api, auth")
    monkeypatch.setenv("CRITICAL_PATTERNS", "checkout failed,[bad(")  # the broken regex is ignored
    assert severity.apply({"service": "Payment-API", "error": "x"}) == {"severity": "CRITICAL"}
    assert severity.apply({"service": "web", "error": "Checkout FAILED for order 9"}) == {"severity": "CRITICAL"}
    assert severity.apply({"service": "web", "error": "slow page"}) == {}


def test_critical_routes_to_incident():
    assert router_rules.decide({"severity": "CRITICAL"}, ["incident", "monitor"])["agent"] == "incident"


def test_critical_signal_is_stored_critical_and_paged(tmp_path, monkeypatch):
    monkeypatch.setenv("CRITICAL_SERVICES", "payment-api")
    pages = []
    monkeypatch.setattr("sre_agent.core.paging.page", lambda t, b, s="CRITICAL": pages.append((t, s)) or True)
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: None)
    log = tmp_path / "app.log"
    log.write_text(f"{TS} [ERROR] [payment-api] TypeError: boom in charge()\n")
    out = monitor.run(source=str(log), mode="once")
    assert "CRITICAL" in out and pages and pages[0][1] == "CRITICAL"


# ---- quiet hours ----


def test_quiet_hours_wrap_midnight_and_plain_ranges(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS", "22:00-07:00")
    at = lambda h, m=0: datetime(2026, 1, 1, h, m)  # noqa: E731
    assert [severity.in_quiet_hours(at(*t)) for t in [(23,), (3,), (6, 59), (7,), (12,)]] == [True, True, True, False, False]
    monkeypatch.setenv("QUIET_HOURS", "01:00-05:00")
    assert [severity.in_quiet_hours(at(h)) for h in (0, 1, 4, 5)] == [False, True, True, False]
    monkeypatch.setenv("QUIET_HOURS", "nonsense")
    assert severity.in_quiet_hours(at(3)) is False


def test_only_signals_below_the_floor_are_held(monkeypatch):
    monkeypatch.setenv("QUIET_HOURS", "00:00-23:59")
    now = datetime(2026, 1, 1, 12, 0)
    assert severity.should_hold({"severity": "MEDIUM"}, now) is True
    assert severity.should_hold({"severity": "HIGH"}, now) is False
    monkeypatch.setenv("QUIET_MIN_SEVERITY", "CRITICAL")
    assert severity.should_hold({"severity": "HIGH"}, now) is True
    assert severity.should_hold({"severity": "CRITICAL"}, now) is False


def test_held_signal_is_stored_not_announced_then_released_once_per_issue(tmp_path, monkeypatch):
    announced = []
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: announced.append(a))
    monkeypatch.setattr("sre_agent.sre.severity.in_quiet_hours", lambda now=None: True)
    log = tmp_path / "app.log"
    log.write_text(f"{TS} [WARN] [api] cache is slow\n")
    out = monitor.run(source=str(log), mode="once")
    assert out.startswith("no new signals") and "held for quiet hours" in out and not announced
    with open(log, "a") as f:
        f.write(f"{TS} [WARN] [api] cache is slow\n")
    monitor.run(source=str(log), mode="once")
    held = list_items(kind="signal", status="held")
    assert len(held) >= 1

    handed = []
    monkeypatch.setattr(auto, "_on_signal", lambda sid: handed.append(sid))
    assert auto.release_held() == []  # still quiet: nothing happens
    monkeypatch.setattr("sre_agent.sre.severity.in_quiet_hours", lambda now=None: False)
    released = auto.release_held()
    assert len(released) == 1 and handed == released  # one card per issue, not one per held signal
    assert not list_items(kind="signal", status="held")
    assert get_item(released[0])["status"] == "new"

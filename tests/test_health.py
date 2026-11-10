"""Health checks: flap control, monitor liveness, log silence, URL/TCP/disk/memory/TLS/systemd. Offline."""
import socket
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent.sre import health

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def sent(monkeypatch):
    msgs, pages = [], []
    monkeypatch.setattr("sre_agent.sre.health.notify", lambda t, b, buttons=None: msgs.append((t, b)))
    monkeypatch.setattr("sre_agent.core.paging.page", lambda t, b, s="CRITICAL": pages.append((t, s)) or True)
    monkeypatch.setenv("HEALTH_FAILS_BEFORE_ALERT", "1")
    return msgs, pages


def test_needs_consecutive_failures_then_alerts_once_per_cooldown_then_recovers(monkeypatch):
    msgs = []
    monkeypatch.setattr("sre_agent.sre.health.notify", lambda t, b, buttons=None: msgs.append((t, b)))
    monkeypatch.setenv("HEALTH_FAILS_BEFORE_ALERT", "2")
    assert health.report("k", False, "Thing down", "why", now=T0) == "failing" and not msgs
    assert health.report("k", False, "Thing down", "why", now=T0) == "alerted"
    assert msgs[0][0] == "action needed: sre-health HIGH Thing down" and "why" in msgs[0][1]
    assert health.report("k", False, "Thing down", "why", now=T0 + timedelta(minutes=5)) == "quiet"
    assert health.report("k", False, "Thing down", "why", now=T0 + timedelta(hours=2)) == "alerted"  # cooldown over
    assert health.report("k", True, "Thing down") == "recovered" and msgs[-1][0].startswith("recovered:")
    assert health.report("k", True, "Thing down") == "ok" and len(msgs) == 3


def test_a_blip_between_failures_resets_the_count(monkeypatch):
    monkeypatch.setattr("sre_agent.sre.health.notify", lambda *a, **k: None)
    monkeypatch.setenv("HEALTH_FAILS_BEFORE_ALERT", "2")
    assert health.report("b", False, "x", now=T0) == "failing"
    assert health.report("b", True, "x") == "ok"
    assert health.report("b", False, "x", now=T0) == "failing"  # counting starts over


def test_alert_is_one_shot_and_pages_only_when_asked(sent):
    msgs, pages = sent
    assert health.alert("a", "Disk", "d", page=True) is True
    assert health.alert("a", "Disk", "d", page=True) is False
    assert len(msgs) == 1 and pages == [("sre-health: Disk", "CRITICAL")]
    health.alert("quiet-one", "Minor", "d")
    assert len(pages) == 1


def test_monitor_stale_and_check_monitor(sent, monkeypatch):
    monkeypatch.setattr(health, "_last_poll", 1000.0)
    assert health.monitor_stale(now=1010.0) is False
    assert health.monitor_stale(now=1000.0 + 10_000) is True
    msgs, _ = sent
    monkeypatch.setattr(health, "_last_poll", time.monotonic() - 10_000)
    health.check_monitor()
    assert "Log monitor stopped reading" in msgs[0][0]
    health.poll(True)
    health.check_monitor()
    assert msgs[-1][0].startswith("recovered:")


def test_log_silence_is_off_by_default_and_alerts_when_enabled(sent, monkeypatch):
    msgs, _ = sent
    monkeypatch.setattr(health, "_last_text", time.monotonic() - 3600)
    assert health.check_silence() is None and not msgs
    monkeypatch.setenv("LOG_SILENCE_MINUTES", "10")
    health.check_silence()
    assert "gone silent" in msgs[0][0]
    health.poll(True)
    health.check_silence()
    assert msgs[-1][0].startswith("recovered:")


def test_url_check(sent, monkeypatch):
    msgs, pages = sent
    monkeypatch.setenv("HEALTH_URLS", "http://app.internal/health")
    monkeypatch.setattr("sre_agent.sre.health.httpx.get", lambda *a, **k: namedtuple("R", "status_code")(503))
    health.check_urls()
    assert "Health URL failing" in msgs[0][0] and "503" in msgs[0][1] and pages
    monkeypatch.setattr("sre_agent.sre.health.httpx.get", lambda *a, **k: namedtuple("R", "status_code")(200))
    health.check_urls()
    assert msgs[-1][0].startswith("recovered:")


def test_tcp_check_detects_a_closed_port(sent, monkeypatch):
    msgs, _ = sent
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("HEALTH_TCP", f"127.0.0.1:{port}")
    health.check_tcp()
    assert "Service not reachable" in msgs[0][0]


def test_disk_and_memory(sent, monkeypatch, tmp_path):
    msgs, _ = sent
    usage = namedtuple("U", "total used free")
    monkeypatch.setenv("HEALTH_DISK_PATHS", str(tmp_path))
    monkeypatch.setattr("sre_agent.sre.health.shutil.disk_usage", lambda p: usage(100 * 2**20, 95 * 2**20, 5 * 2**20))
    health.check_disk()
    assert "Disk almost full" in msgs[0][0] and "5.0% free" in msgs[0][1]
    sample = "MemTotal:       1000000 kB\nMemFree: 1 kB\nMemAvailable:    50000 kB\n"
    assert health.memory_available_pct(sample) == pytest.approx(5.0)
    assert health.memory_available_pct("garbage") is None
    monkeypatch.setattr(health, "memory_available_pct", lambda *a: 5.0)
    health.check_memory()
    assert "memory almost exhausted" in msgs[-1][0]


def test_tls_expiry_and_handshake_failure(sent, monkeypatch):
    msgs, _ = sent
    monkeypatch.setenv("HEALTH_TLS_HOSTS", "example.test")
    monkeypatch.setattr(health, "cert_days_left", lambda host, port=443: 3.0)
    health.check_tls()
    assert "TLS certificate problem" in msgs[0][0] and "3 day" in msgs[0][1]
    monkeypatch.setattr(health, "cert_days_left", lambda host, port=443: 90.0)
    health.check_tls()
    assert msgs[-1][0].startswith("recovered:")

    def boom(host, port=443):
        raise OSError("certificate has expired")

    monkeypatch.setattr(health, "cert_days_left", boom)
    health.check_tls()
    assert "check failed" in msgs[-1][1]


def test_systemd_inactive_and_crash_loop(sent, monkeypatch):
    msgs, pages = sent
    monkeypatch.setenv("HEALTH_SYSTEMD_UNITS", "myapp.service;rm -rf /,myapp.service")  # the odd name is ignored
    state = {"restarts": 0, "active": "active"}

    def fake(*args):
        return state["active"] if args[0] == "is-active" else str(state["restarts"])

    monkeypatch.setattr(health, "_systemctl", fake)
    health.check_systemd()
    assert not msgs
    state["restarts"] = 5
    health.check_systemd()
    assert any("restarting repeatedly" in m[0] for m in msgs) and pages
    state["active"] = "failed"
    health.check_systemd()
    assert any("Service not running" in m[0] for m in msgs)


def test_one_crashing_check_does_not_stop_the_rest(sent, monkeypatch):
    called = []
    monkeypatch.setattr(health, "check_urls", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(health, "check_disk", lambda: called.append("disk"))
    monkeypatch.setattr(health, "ping_heartbeat", lambda: called.append("ping"))
    health.run_checks()
    assert "disk" in called and "ping" in called


def test_heartbeat_ping(monkeypatch):
    urls = []
    monkeypatch.setenv("HEALTH_HEARTBEAT_URL", "https://hc.example/ping/abc")
    monkeypatch.setattr("sre_agent.sre.health.httpx.get", lambda url, **k: urls.append(url))
    health.ping_heartbeat()
    assert urls == ["https://hc.example/ping/abc"]

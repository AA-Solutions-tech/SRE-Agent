"""Health of the agent and of the app around it: the failures that leave no error line to read.

Reading logs cannot see an app that crashed, a watcher that stopped, a full disk or an expiring certificate.
This module raises those as "action needed" notices, with flap control (N consecutive failures before an alert,
one alert per cooldown, one "recovered" message) so a blip never pages.

    report(key, ok, title, detail)   record one check result; alerts / recovers by itself
    poll(had_text)                   the monitor calls this every poll (liveness + log-silence tracking)
    run_checks()                     one pass of every configured check; loop() repeats it forever

Configuration (all optional; a check that is not configured does not run):
    HEALTH_INTERVAL_SECONDS (60)   HEALTH_FAILS_BEFORE_ALERT (2)   HEALTH_ALERT_COOLDOWN_MINUTES (60)
    HEALTH_URLS            comma list of http(s) URLs that must answer < 400
    HEALTH_TCP             comma list of host:port that must accept connections
    HEALTH_DISK_PATHS      comma list of paths (default: the agent data dir)      HEALTH_MIN_DISK_PCT (10)
    HEALTH_MIN_MEM_PCT     (10)  Linux only: alert when available memory falls below this percent
    HEALTH_TLS_HOSTS       comma list of host[:port]; alert before expiry         HEALTH_TLS_WARN_DAYS (14)
    HEALTH_SYSTEMD_UNITS   comma list of units that must be active; crash-loop detection  HEALTH_CRASH_RESTARTS (3)
    LOG_SILENCE_MINUTES    alert when the monitored log has produced no new line for this long (0 = off)
    HEALTH_HEARTBEAT_URL   pinged every pass (dead-man switch: an external service alerts if the agent dies)
"""
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from sre_agent.core import paging
from sre_agent.core.config import settings
from sre_agent.core.db import create_item, find_by_field, update_item
from sre_agent.core.notify import notify
from sre_agent.core.sre_log import get_logger, step

logger = get_logger("health")
KIND = "health_state"
SERVICE = "sre-health"
NL = chr(10)
_lock = threading.RLock()
_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:-]+$")
_started = time.monotonic()
_last_poll = 0.0
_last_text = time.monotonic()


def _int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, default)))
    except ValueError:
        return default


def _csv(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------- state + alerting ----------


def _state(key: str) -> dict | None:
    item = find_by_field(KIND, "key", key)
    return {"id": item["id"], **item["data"]} if item else None


def _save(key: str, **changes) -> dict:
    state = _state(key)
    if state is None:
        data = {"key": key, "fails": 0, "down": False, "last_alert": None, "last_value": None, **changes}
        return {"id": create_item(KIND, data), **data}
    update_item(state["id"], **changes)
    return {**state, **changes}


def _send(title: str, detail: str, page: bool, status: str) -> None:
    body = NL.join(["*ERROR*", title, "", "*DETAILS*", detail])
    notify(f"action needed: {SERVICE} HIGH {title}" if status == "down" else f"recovered: {SERVICE} {title}", body)
    if page and status == "down":
        paging.page(f"{SERVICE}: {title}", detail, "CRITICAL")


def report(key: str, ok: bool, title: str, detail: str = "", *, fails_needed: int | None = None,
           page: bool = False, cooldown_minutes: float | None = None, now: datetime | None = None) -> str:
    """Record one result for check `key`. Returns "ok", "recovered", "failing", "alerted" or "quiet"."""
    now = now or _now()
    needed = fails_needed if fails_needed is not None else max(1, _int("HEALTH_FAILS_BEFORE_ALERT", 2))
    cool = timedelta(minutes=cooldown_minutes if cooldown_minutes is not None
                     else float(os.getenv("HEALTH_ALERT_COOLDOWN_MINUTES", 60) or 60))
    with _lock:
        st = _state(key) or _save(key)
        if ok:
            if st.get("down"):
                _save(key, fails=0, down=False)
                _send(title, "Back to normal.", False, "recovered")
                step(logger, "health.02.recovered", "check recovered", key=key)
                return "recovered"
            if st.get("fails"):
                _save(key, fails=0)
            return "ok"
        fails = int(st.get("fails") or 0) + 1
        last = _parse(st.get("last_alert"))
        if fails < needed:
            _save(key, fails=fails)
            return "failing"
        if st.get("down") and last and now - last < cool:
            _save(key, fails=fails)
            return "quiet"
        _save(key, fails=fails, down=True, last_alert=now.isoformat())
    _send(title, detail, page, "down")
    step(logger, "health.01.alert", "health alert sent", level="warning", key=key, title=title)
    return "alerted"


def alert(key: str, title: str, detail: str, *, page: bool = False, cooldown_minutes: float | None = None) -> bool:
    """One-shot alert (no consecutive-failure counting), at most once per cooldown. True when it was sent."""
    return report(key, False, title, detail, fails_needed=1, page=page, cooldown_minutes=cooldown_minutes) == "alerted"


# ---------- monitor liveness and log silence ----------


def poll(had_text: bool) -> None:
    """Called by the monitor after every poll."""
    global _last_poll, _last_text
    _last_poll = time.monotonic()
    if had_text:
        _last_text = _last_poll


def monitor_stale(now: float | None = None) -> bool:
    """The monitor has not completed a poll lately (never started, hung or dead)."""
    limit = max(120, 5 * settings.poll_seconds)
    return (now or time.monotonic()) - (_last_poll or _started) > limit


def check_monitor() -> str:
    return report("monitor_poll", not monitor_stale(), "Log monitor stopped reading",
                  "The monitor has not completed a poll recently, so new errors are NOT being seen. "
                  "Check the agent's log, the log path / SSH connection, and restart the service.", page=True)


def check_silence() -> str | None:
    minutes = _int("LOG_SILENCE_MINUTES", 0)
    if not minutes:
        return None
    quiet_for = (time.monotonic() - _last_text) / 60
    return report("log_silence", quiet_for < minutes, "Application log has gone silent",
                  f"No new log line for {quiet_for:.0f} min (limit {minutes}). The app may have crashed or "
                  "stopped logging, or the log path changed.", page=True)


# ---------- active checks ----------


def check_urls() -> list[str]:
    out = []
    for url in _csv("HEALTH_URLS"):
        try:
            code = httpx.get(url, timeout=5, follow_redirects=True).status_code
            ok, why = code < 400, f"{url} answered HTTP {code}"
        except Exception as e:  # noqa: BLE001
            ok, why = False, f"{url} did not answer ({type(e).__name__})"
        out.append(report(f"url:{url}", ok, f"Health URL failing: {url}", why, page=True))
    return out


def check_tcp() -> list[str]:
    out = []
    for target in _csv("HEALTH_TCP"):
        host, _, port = target.rpartition(":")
        try:
            with socket.create_connection((host, int(port)), timeout=5):
                ok, why = True, ""
        except (OSError, ValueError) as e:
            ok, why = False, f"{target} refused or timed out ({type(e).__name__})"
        out.append(report(f"tcp:{target}", ok, f"Service not reachable: {target}", why, page=True))
    return out


def check_disk() -> list[str]:
    floor, out = _int("HEALTH_MIN_DISK_PCT", 10), []
    for path in _csv("HEALTH_DISK_PATHS") or [str(settings.data_dir)]:
        try:
            u = shutil.disk_usage(path)
        except OSError:
            continue
        free = u.free * 100 / u.total
        out.append(report(f"disk:{path}", free >= floor, f"Disk almost full: {path}",
                          f"{free:.1f}% free ({u.free // 2**20} MiB), limit {floor}%."))
    return out


def memory_available_pct(meminfo: str | None = None) -> float | None:
    try:
        text = meminfo if meminfo is not None else open("/proc/meminfo", encoding="utf-8").read()
        vals = {k: int(v.split()[0]) for k, v in (ln.split(":", 1) for ln in text.splitlines() if ":" in ln)}
        return vals["MemAvailable"] * 100 / vals["MemTotal"]
    except (OSError, KeyError, ValueError):
        return None  # not Linux / unreadable: skip


def check_memory() -> str | None:
    pct = memory_available_pct()
    floor = _int("HEALTH_MIN_MEM_PCT", 10)
    if pct is None or not floor:
        return None
    return report("memory", pct >= floor, "Server memory almost exhausted",
                  f"{pct:.1f}% of memory available, limit {floor}%.")


def cert_days_left(host: str, port: int = 443) -> float:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=5) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
            cert = tls.getpeercert()
    return (ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) / 86400


def check_tls() -> list[str]:
    out, warn = [], _int("HEALTH_TLS_WARN_DAYS", 14)
    for target in _csv("HEALTH_TLS_HOSTS"):
        host, _, port = target.partition(":")
        try:
            days = cert_days_left(host, int(port or 443))
            ok, why = days >= warn, f"{host} certificate expires in {days:.0f} day(s) (warn at {warn})."
        except (OSError, ValueError) as e:  # includes ssl.SSLError (expired / invalid / wrong host)
            ok, why = False, f"{host} TLS check failed: {type(e).__name__}: {str(e)[:120]}"
        out.append(report(f"tls:{target}", ok, f"TLS certificate problem: {host}", why))
    return out


def _systemctl(*args: str) -> str:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=10).stdout.strip()


def check_systemd() -> list[str]:
    out, limit = [], _int("HEALTH_CRASH_RESTARTS", 3)
    for unit in _csv("HEALTH_SYSTEMD_UNITS"):
        if not _UNIT_RE.match(unit):
            continue
        try:
            active = _systemctl("is-active", unit)
            restarts = int(_systemctl("show", unit, "-p", "NRestarts", "--value") or 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue  # no systemd here
        out.append(report(f"unit:{unit}", active == "active", f"Service not running: {unit}",
                          f"systemctl reports `{active or 'unknown'}`.", page=True))
        with _lock:
            st = _state(f"restarts:{unit}") or _save(f"restarts:{unit}")
            previous = st.get("last_value")
            _save(f"restarts:{unit}", last_value=restarts)
        looping = previous is not None and restarts - int(previous) >= limit
        out.append(report(f"crashloop:{unit}", not looping, f"Service restarting repeatedly: {unit}",
                          f"{restarts - int(previous or 0)} restart(s) since the last check.", fails_needed=1, page=True))
    return out


def ping_heartbeat() -> None:
    url = os.getenv("HEALTH_HEARTBEAT_URL", "").strip()
    if url:
        try:
            httpx.get(url, timeout=5)
        except Exception as e:  # noqa: BLE001
            step(logger, "health.03.heartbeat", "heartbeat ping failed", level="warning", error=type(e).__name__)


def run_checks(include_monitor: bool = True) -> None:
    """One pass of every configured check. A crashing check never stops the others."""
    checks = [check_urls, check_tcp, check_disk, check_memory, check_tls, check_systemd, check_silence]
    if include_monitor:
        checks.insert(0, check_monitor)
    for check in checks:
        try:
            check()
        except Exception as e:  # noqa: BLE001
            step(logger, "health.04.check", "health check crashed", level="error", check=check.__name__,
                 error=f"{type(e).__name__}: {e}"[:200])
    ping_heartbeat()


def loop() -> None:
    while True:
        run_checks()
        time.sleep(max(5, _int("HEALTH_INTERVAL_SECONDS", 60)))

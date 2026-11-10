"""Volume anomalies: a sudden error spike, or several brand-new error types at once (typical of a bad deploy).

Each distinct error still gets its own card once; this watches the volume across all of them, which no single
card shows. State is in memory (it only needs the last hour and rebuilds itself after a restart).

    SPIKE_WINDOW_MINUTES (5)    SPIKE_BASELINE_MINUTES (60)   SPIKE_FACTOR (5)   SPIKE_MIN_ERRORS (20)
    NEW_ISSUE_WINDOW_MINUTES (10)   NEW_ISSUE_BURST (3)       ANOMALY_COOLDOWN_MINUTES (30)
"""
import os
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

from sre_agent.sre import health

_lock = threading.Lock()
_errors: deque = deque()      # (time, error lines in that poll)
_new_issues: deque = deque()  # (time, new distinct issues in that poll)


def _num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _prune(q: deque, cutoff: datetime) -> None:
    while q and q[0][0] < cutoff:
        q.popleft()


def _latest_commit() -> str:
    """Newest commit in REPO_DIR, as a hint for 'did something just deploy?'. Empty when unavailable."""
    try:
        from sre_agent.core.git_tools import _git
        from sre_agent.sre.incident import code_inspect

        repo = code_inspect._repo()
        if repo is None:
            return ""
        out = _git(repo, "log", "-1", "--format=%h %cI %s", check=False).strip()
        return f"Latest commit in the repo: {out}" if out else ""
    except Exception:  # noqa: BLE001
        return ""


def check_spike(now: datetime | None = None) -> str | None:
    """Alert when the last window holds far more errors than the baseline hour. Returns the alert text."""
    now = now or _now()
    window = timedelta(minutes=_num("SPIKE_WINDOW_MINUTES", 5))
    base = timedelta(minutes=_num("SPIKE_BASELINE_MINUTES", 60))
    with _lock:
        _prune(_errors, now - window - base)
        recent = sum(n for t, n in _errors if t > now - window)
        before = sum(n for t, n in _errors if t <= now - window)
    per_window = before / max(1.0, base / window)
    if recent < _num("SPIKE_MIN_ERRORS", 20) or recent < _num("SPIKE_FACTOR", 5) * max(per_window, 1.0):
        return None
    detail = (f"{recent} error line(s) in the last {window.total_seconds() / 60:g} min, against a normal "
              f"{per_window:.1f} per {window.total_seconds() / 60:g} min. {_latest_commit()}").strip()
    health.alert("error_spike", "Error spike", detail, page=True,
                 cooldown_minutes=_num("ANOMALY_COOLDOWN_MINUTES", 30))
    return detail


def record_errors(n: int, now: datetime | None = None) -> str | None:
    if n <= 0:
        return None
    now = now or _now()
    with _lock:
        _errors.append((now, n))
    return check_spike(now)


def record_new_issues(n: int, now: datetime | None = None) -> str | None:
    """Several new distinct errors in a short time: something changed (deploy, config, dependency)."""
    if n <= 0:
        return None
    now = now or _now()
    window = timedelta(minutes=_num("NEW_ISSUE_WINDOW_MINUTES", 10))
    with _lock:
        _new_issues.append((now, n))
        _prune(_new_issues, now - window)
        total = sum(c for _, c in _new_issues)
    if total < _num("NEW_ISSUE_BURST", 3):
        return None
    detail = (f"{total} new kinds of error appeared within {window.total_seconds() / 60:g} min. "
              f"That usually means a change: a deploy, a config edit or a dependency failing. "
              f"{_latest_commit()}").strip()
    health.alert("new_issue_burst", "Several new errors at once", detail,
                 cooldown_minutes=_num("ANOMALY_COOLDOWN_MINUTES", 30))
    return detail


def reset() -> None:
    with _lock:
        _errors.clear()
        _new_issues.clear()

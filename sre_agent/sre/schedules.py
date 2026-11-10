"""Scheduled-job watch: raise a signal when a job that should have run did not.

Errors only show up in logs when a job runs and fails. A cron job that never starts logs nothing, so the
monitor alone is blind to it. Here each job declares when it is due and which log line proves it ran:

    SRE_JOBS_FILE (default <SRE_HOME>/jobs.json)
    {"jobs": [{"name": "weekly-timesheet-reminder", "service": "timesheet",
               "cron": "0 9 * * 1",            # 5 fields: min hour day-of-month month day-of-week
               "match": "timesheet reminder job started",   # text (or regex) of a line the job always logs
               "regex": false, "grace_minutes": 15}]}

Optional completion tracking (catches a job that started and hung, or finished only part of its work):
               "done_match": "timesheet reminders sent",     # text (or regex with "done_regex": true)
               "max_minutes": 30,                            # started but no done line within this long = unfinished
    A done regex may name groups `done` and `total` (e.g. "sent (?P<done>\d+) of (?P<total>\d+)"); done < total
    is reported as a partial run.

Pick a line the job logs when it STARTS (not one that only appears on success), so a job that ran and failed
raises its own error signal instead of also being reported as missed.

  observe(lines)     called by the monitor for every batch of new log lines (also marks the monitor alive)
  check_missed()     called periodically: files a signal (category job_missed) per missed run, once per run
"""
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sre_agent.core.config import settings
from sre_agent.core.db import create_item, find_by_field, update_item
from sre_agent.core.notify import notify
from sre_agent.core.sre_log import bind_trace, get_logger, step
from sre_agent.sre import issues

logger = get_logger("schedules")
STATE_KIND = "job_state"
DEFAULT_GRACE_MINUTES = 15
NL = chr(10)
_lock = threading.RLock()
_last_poll = 0.0  # time.monotonic() of the last monitor poll (0 = monitor never ran)
_cache: dict = {"key": None, "jobs": []}


# ---------- cron ----------


@dataclass(frozen=True)
class Cron:
    minutes: frozenset
    hours: frozenset
    days: frozenset
    months: frozenset
    weekdays: frozenset  # 0 = Sunday
    any_day: bool
    any_weekday: bool

    def day_matches(self, day) -> bool:
        if day.month not in self.months:
            return False
        dow_ok = (day.weekday() + 1) % 7 in self.weekdays
        dom_ok = day.day in self.days
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return dow_ok
        if self.any_weekday:
            return dom_ok
        return dom_ok or dow_ok  # classic cron: both restricted means either


def _field(spec: str, lo: int, hi: int) -> frozenset:
    out: set[int] = set()
    for part in spec.split(","):
        base, slash, step_text = part.partition("/")
        stride = int(step_text) if slash else 1
        if stride < 1:
            raise ValueError(f"bad step in {spec!r}")
        if base == "*":
            a, b = lo, hi
        elif "-" in base:
            a, b = (int(x) for x in base.split("-", 1))
        else:
            a = int(base)
            b = hi if slash else a
        if a < lo or b > hi or a > b:
            raise ValueError(f"{spec!r} out of range {lo}-{hi}")
        out.update(range(a, b + 1, stride))
    return frozenset(out)


def parse_cron(expr: str) -> Cron:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("cron needs 5 fields: minute hour day-of-month month day-of-week")
    minute, hour, dom, month, dow = fields
    weekdays = frozenset(d % 7 for d in _field(dow, 0, 7))
    return Cron(_field(minute, 0, 59), _field(hour, 0, 23), _field(dom, 1, 31), _field(month, 1, 12),
                weekdays, dom.startswith("*"), dow.startswith("*"))


def zone(name: str | None):
    """A timezone by IANA name ("UTC", "Asia/Kolkata"), or None for the server's own local time."""
    name = (name or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"unknown timezone {name!r} (use a name like UTC or Asia/Kolkata)") from None


def last_due(cron: Cron, now: datetime, lookback_days: int = 400, tz=None) -> datetime | None:
    """Most recent scheduled time at or before `now`, as a timezone-aware time. The schedule is read in `tz`
    (the job's timezone), or in the server's local time when `tz` is None."""
    times = sorted(((h, m) for h in cron.hours for m in cron.minutes), reverse=True)
    base = now.astimezone(tz) if tz else now
    for back in range(lookback_days):
        day = (base - timedelta(days=back)).date()
        if not cron.day_matches(day):
            continue
        for h, m in times:
            candidate = datetime.combine(day, dtime(h, m), tzinfo=tz) if tz else datetime.combine(day, dtime(h, m)).astimezone()
            if candidate <= now:
                return candidate
    return None


# ---------- job definitions ----------


@dataclass(frozen=True)
class Job:
    name: str
    cron_text: str
    cron: Cron
    match: str
    service: str
    grace: timedelta
    regex: re.Pattern | None
    done_match: str = ""
    done_regex: re.Pattern | None = None
    max_run: timedelta = timedelta(minutes=30)
    tz: object = None      # timezone the cron schedule is written in (default: server local time)
    log_tz: object = None  # timezone the app's log lines are stamped in when they carry no offset (default: local)

    def matches(self, line: str) -> bool:
        if self.regex is not None:
            return bool(self.regex.search(line))
        return self.match.lower() in line.lower()

    def done_hit(self, line: str):
        """The match object for a completion line (or True for plain text), else None."""
        if not self.done_match:
            return None
        if self.done_regex is not None:
            return self.done_regex.search(line)
        return True if self.done_match.lower() in line.lower() else None


def jobs_path() -> Path:
    return Path(os.getenv("SRE_JOBS_FILE", "").strip() or settings.data_dir / "jobs.json")


def _build_job(raw: dict) -> Job:
    name, match = str(raw.get("name", "")).strip(), str(raw.get("match", "")).strip()
    if not name or not match:
        raise ValueError("a job needs a name and a match")
    pattern = re.compile(match, re.I) if raw.get("regex") else None
    grace = float(raw.get("grace_minutes", DEFAULT_GRACE_MINUTES))
    done = str(raw.get("done_match", "")).strip()
    done_pattern = re.compile(done, re.I) if done and raw.get("done_regex") else None
    return Job(name=name, cron_text=str(raw.get("cron", "")), cron=parse_cron(str(raw.get("cron", ""))),
               match=match, service=str(raw.get("service") or name), grace=timedelta(minutes=grace),
               regex=pattern, done_match=done, done_regex=done_pattern,
               max_run=timedelta(minutes=float(raw.get("max_minutes", 30))),
               tz=zone(raw.get("timezone") or os.getenv("JOBS_TIMEZONE")),
               log_tz=zone(raw.get("log_timezone") or os.getenv("LOG_TIMEZONE")))


def load_jobs() -> list[Job]:
    """Jobs from the jobs file (re-read when the file changes). A bad entry is skipped and logged."""
    path = jobs_path()
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return []
    if _cache["key"] == key:
        return _cache["jobs"]
    jobs: list[Job] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("jobs", []) if isinstance(data, dict) else data
    except (OSError, ValueError) as e:
        step(logger, "schedules.01.load", "jobs file unreadable", level="error", path=str(path),
             error=f"{type(e).__name__}: {e}"[:200])
        entries = []
    for raw in entries if isinstance(entries, list) else []:
        try:
            jobs.append(_build_job(raw))
        except (ValueError, TypeError, re.error) as e:
            step(logger, "schedules.01.load", "job skipped", level="error",
                 job=str(raw.get("name") if isinstance(raw, dict) else raw)[:60], error=str(e)[:200])
    _cache.update(key=key, jobs=jobs)
    step(logger, "schedules.01.load", "jobs loaded", path=str(path), jobs=[j.name for j in jobs])
    return jobs


# ---------- state ----------


def _state(name: str) -> dict | None:
    item = find_by_field(STATE_KIND, "name", name)
    return {"id": item["id"], **item["data"]} if item else None


def _ensure_state(name: str, now: datetime) -> dict:
    state = _state(name)
    if state is None:
        data = {"name": name, "watching_since": now.isoformat(), "last_seen": None, "alerted_due": None}
        state = {"id": create_item(STATE_KIND, data), **data}
    return state


def _parse_dt(value, tz=None) -> datetime | None:
    """ISO time to an aware datetime. A time with no offset is read in `tz` (server local time when None)."""
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo:
            return dt
        return dt.replace(tzinfo=tz) if tz else dt.astimezone()
    except ValueError:
        return None


def _line_time(line: str, now: datetime, tz=None) -> datetime:
    """Time stamped on the log line (no offset = `tz`, else server local time); `now` when it has none or is in the
    future. An app that logs UTC on a server in another zone needs LOG_TIMEZONE (or the job's log_timezone), or its
    lines look hours old and a job that ran is reported as missed."""
    from sre_agent.sre.monitor.parse import parse_line

    ts = parse_line(line).get("ts")
    if ts:
        text = ts.strip().replace("Z", "+00:00")
        if re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d+$", text):
            text = text[:19] + "." + text[20:]  # node logger writes 13:18:04:184
        parsed = _parse_dt(text, tz)
        if parsed and parsed <= now + timedelta(minutes=5):
            return parsed
    return now


# ---------- monitor side ----------


def observe(lines: list[str], now: datetime | None = None) -> None:
    """Record, per job, the newest log line that proves it ran. Never raises (monitoring must not break)."""
    global _last_poll
    _last_poll = time.monotonic()  # a poll happened: "no line seen" now means something
    try:
        jobs = load_jobs()
        if not jobs or not lines:
            return
        now = now or datetime.now().astimezone()
        with _lock:
            for job in jobs:
                seen = [_line_time(line, now, job.log_tz) for line in lines if job.matches(line)]
                finished = [(_line_time(line, now, job.log_tz), hit) for line in lines if (hit := job.done_hit(line))]
                if not seen and not finished:
                    continue
                state = _ensure_state(job.name, now)
                if seen:
                    newest, previous = max(seen), _parse_dt(state.get("last_seen"))
                    if previous is None or newest > previous:
                        update_item(state["id"], last_seen=newest.isoformat())
                if finished:
                    when, hit = max(finished, key=lambda f: f[0])
                    previous_done = _parse_dt(state.get("last_done"))
                    if previous_done is None or when > previous_done:
                        counts = hit.groupdict() if hasattr(hit, "groupdict") else {}
                        update_item(state["id"], last_done=when.isoformat(), done_count=_int(counts.get("done")),
                                    done_total=_int(counts.get("total")))
    except Exception as e:  # noqa: BLE001
        step(logger, "schedules.02.observe", "observe failed", level="error", error=f"{type(e).__name__}: {e}"[:200])


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _monitor_alive() -> bool:
    return _last_poll > 0 and time.monotonic() - _last_poll < max(120, 5 * settings.poll_seconds)


# ---------- checker ----------


ERROR_TEXT = {"missed": "Scheduled job missed", "unfinished": "Scheduled job did not finish",
              "partial": "Scheduled job finished partially"}


def _file_signal(job: Job, kind: str, now: datetime, evidence: str, **details) -> str | None:
    """Store a job_missed signal (kind: missed | unfinished | partial) and notify.
    Returns the signal id, or None for a known (counted) issue."""
    signal = {
        "service": job.service,
        "severity": "HIGH",
        "error": f"{ERROR_TEXT[kind]}: {job.name}",
        "evidence": [evidence],
        "ts": now.isoformat(),
        "source": "scheduler",
        "category": "job_missed",
        "details": {"kind": kind, "job": job.name, "cron": job.cron_text, "match": job.match,
                    "grace_minutes": job.grace.total_seconds() / 60, **details},
    }
    signal["fingerprint"] = issues.fingerprint(job.service, signal["error"])
    signal_id = create_item("signal", signal)
    bind_trace(signal_id)
    issue_id, action = issues.record_occurrence(signal, signal_id)
    update_item(signal_id, issue_id=issue_id)
    if action == "count":
        return None
    notify(f"signal detected: {job.service} HIGH [trace {signal_id}]",
           NL.join(["*ERROR*", signal["error"], "", "*DETAILS*", f"{evidence} · signal `{signal_id}`"]))
    step(logger, "schedules.03.missed", signal["error"], signal_id=signal_id, job=job.name, kind=kind,
         level="warning")
    return signal_id


def _file_missed(job: Job, due: datetime, last_seen: str | None, now: datetime) -> str | None:
    expected = due.strftime("%Y-%m-%d %H:%M")
    return _file_signal(
        job, "missed", now,
        f"{job.name} ({job.cron_text}) was due {expected}; no log line matching '{job.match}' within "
        f"{job.grace.total_seconds() / 60:g} min; last seen {last_seen or 'never since the agent started watching'}",
        expected_at=due.isoformat(), last_seen=last_seen)


def _check_completion(job: Job, state: dict, now: datetime) -> list[str]:
    """Signals for a run that started but never finished, or finished only part of its work."""
    started, done = _parse_dt(state.get("last_seen")), _parse_dt(state.get("last_done"))
    out: list[str] = []
    if (started and (done is None or done < started) and now > started + job.max_run
            and state.get("alerted_unfinished") != started.isoformat()):
        update_item(state["id"], alerted_unfinished=started.isoformat())
        sid = _file_signal(job, "unfinished", now,
                           f"{job.name} started {started:%Y-%m-%d %H:%M} but no `{job.done_match}` line within "
                           f"{job.max_run.total_seconds() / 60:g} min", started_at=started.isoformat(),
                           done_match=job.done_match, max_minutes=job.max_run.total_seconds() / 60)
        out += [sid] if sid else []
    total, count = state.get("done_total"), state.get("done_count")
    if (done and (started is None or done >= started) and total and count is not None and count < total
            and state.get("alerted_partial") != done.isoformat()):
        update_item(state["id"], alerted_partial=done.isoformat())
        sid = _file_signal(job, "partial", now, f"{job.name} finished {done:%Y-%m-%d %H:%M} but only {count} of "
                           f"{total} items completed", finished_at=done.isoformat(), done=count, total=total)
        out += [sid] if sid else []
    return out


def _close_if_recovered(job: Job, state: dict, last_seen: datetime | None) -> None:
    """The job ran again after it was reported missed: close that issue so the next miss gets a fresh card."""
    alerted = state.get("alerted_due")
    if not alerted or not last_seen or state.get("recovered_due") == alerted:
        return
    alerted_at = _parse_dt(alerted)
    if alerted_at is None or last_seen < alerted_at:
        return
    issue = issues.find_by_fingerprint(issues.fingerprint(job.service, f"Scheduled job missed: {job.name}"))
    if issue:
        issues.mark_fixed(issue["id"])
    update_item(state["id"], recovered_due=alerted)


def check_missed(now: datetime | None = None) -> list[str]:
    """Signal ids for jobs whose latest due run produced no matching log line. Once per missed run."""
    jobs = load_jobs()
    if not jobs or not _monitor_alive():  # monitor not polling: silence proves nothing
        return []
    now = now or datetime.now().astimezone()
    filed: list[str] = []
    with _lock:
        for job in jobs:
            state = _ensure_state(job.name, now)
            if job.done_match:
                filed += _check_completion(job, state, now)
            due = last_due(job.cron, now, tz=job.tz)
            if due is None or now < due + job.grace:
                continue
            watching_since = _parse_dt(state.get("watching_since")) or now
            last_seen = _parse_dt(state.get("last_seen"))
            _close_if_recovered(job, state, last_seen)
            if watching_since > due or (last_seen and last_seen >= due):
                continue  # not watching yet when it was due, or it ran
            if state.get("alerted_due") == due.isoformat():
                continue
            update_item(state["id"], alerted_due=due.isoformat())
            signal_id = _file_missed(job, due, state.get("last_seen"), now)
            if signal_id:
                filed.append(signal_id)
    return filed

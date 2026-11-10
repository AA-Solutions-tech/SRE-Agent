"""Monitor agent: tail a log source, file ERROR/WARN as signals, audit the rest.

Pattern: linear LangGraph pipeline  collect -> classify -> emit.
  * The monitor only OBSERVES and REPORTS. It never asks for approval
    (notification only) and never changes anything.
  * Severity is pure Python (parse.py) — no LLM involved, so this agent runs
    fully offline until Groq is wired.

    python run.py monitor --mode once --source data/app.log
    python run.py monitor --mode watch
    python run.py monitor --mode once --source ssh
"""
import os
import time
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from sre_agent.core.config import settings
from sre_agent.core.db import create_item, list_items, update_item
from sre_agent.core import paging
from sre_agent.sre import anomaly, classify as error_kinds, health, ignorelist, issues, schedules
from sre_agent.sre import severity as severity_rules
from sre_agent.core.notify import notify
from sre_agent.core.sre_log import bind_trace, clear_trace, get_logger, step

logger = get_logger("monitor")

from .local_file import LocalFileReader
from .log_source import get_source
from .parse import build_signal, classify
from .sources import expand_path, split_source  # expand_path lives in .sources: the doctor uses it too

CURSOR_KIND = "monitor_cursor"  # one record per watched file: {path, offset}
NL = chr(10)


def _load_offset(path: str) -> int | None:
    """Last read offset for this file (None = never watched)."""
    for item in list_items(kind=CURSOR_KIND):
        if item["data"].get("path") == path:
            return item["data"].get("offset")
    return None


def _save_offset(path: str, offset: int) -> None:
    """Persist the read offset so the next run() continues where we stopped."""
    for item in list_items(kind=CURSOR_KIND):
        if item["data"].get("path") == path:
            update_item(item["id"], offset=offset)
            return
    create_item(CURSOR_KIND, {"path": path, "offset": offset})


class State(TypedDict, total=False):
    text: str | None       # raw new log text (None = read from the live reader)
    source_name: str       # where the text came from (for the signal record)
    signal_lines: list     # parsed ERROR/WARN events
    audit_lines: list      # parsed INFO/DEBUG events (quiet)
    quarantine: list       # unparsable lines (context only, never paging)
    signal_id: str         # first stored signal item id (when a signal fires)
    signal_ids: list       # every signal filed in this pass (one per distinct error)
    summary: str           # human-readable outcome for run()/CLI
    backlog: bool          # this pass replays old log history (MONITOR_REPLAY_BACKLOG): mark its signals
    ignored: int           # lines dropped by the ignore list in this pass


def build(reader=None, service: str | None = None):
    """Compile the monitor graph. reader is the live LogSource (closed over); `service` names its signals.

    Passing text directly in state skips the live read — used by tests.
    """
    def collect(state: State) -> State:
        clear_trace()  # each poll starts untagged; only a filed signal gets a trace
        src = state.get("source_name", "")
        if state.get("text") is not None:
            step(logger, "monitor.04.collect", "using injected text (no live read)", source=src)
            return {}
        try:
            text = reader.read_new() if reader is not None else ""
        except Exception as e:
            step(logger, "monitor.04.collect", "read failed", level="error", source=src,
                 error=f"{type(e).__name__}: {e}")
            raise
        step(logger, "monitor.04.collect", "read new log text", source=src, level="info" if text else "debug",
             chars=len(text), offset=getattr(reader, "offset", None))  # an idle poll is DEBUG: it runs every 5 s
        return {"text": text}

    def classify_node(state: State) -> State:
        text = (state.get("text") or "").strip()
        schedules.observe(text.splitlines())  # proves scheduled jobs ran (and that this monitor is alive)
        health.poll(bool(text))  # liveness + log-silence tracking
        if not text:
            step(logger, "monitor.05.classify", "no new lines to classify", level="debug")
            return {
                "signal_lines": [],
                "audit_lines": [],
                "quarantine": [],
                "ignored": 0,
                "summary": "no new lines",
            }
        parts = classify(text.splitlines(), service)
        parts["signal"], dropped = ignorelist.split(parts["signal"])  # known noise: counted, never a signal
        _safely(ignorelist.record, dropped)
        step(logger, "monitor.05.classify", "classified lines",
             lines=len(text.splitlines()), signal=len(parts["signal"]), ignored=len(dropped),
             audit=len(parts["audit"]), quarantine=len(parts["quarantine"]))
        for q in parts["quarantine"]:
            step(logger, "monitor.05.classify", "unparsable line quarantined", level="warning",
                 raw=q["raw"][:200])
        return {
            "signal_lines": parts["signal"],
            "audit_lines": parts["audit"],
            "quarantine": parts["quarantine"],
            "ignored": len(dropped),
        }

    def emit(state: State) -> State:
        signal_lines = state.get("signal_lines") or []
        audited = len(state.get("audit_lines") or []) + len(
            state.get("quarantine") or []
        )
        for ev in signal_lines:
            step(logger, "monitor.06.error_found", "ERROR/WARN line found", level="warning",
                 level_name=ev["level"], service=ev["service"], ts=ev["ts"],
                 error_text=ev["message"][:200], source=state.get("source_name", ""))
        ignored_note = f", {state['ignored']} ignored" if state.get("ignored") else ""
        if not signal_lines:
            step(logger, "monitor.07.emit", "no signal to file", audited=audited, level="debug")
            logger.debug("signal.none", extra={"source": state.get("source_name", ""), "audited": audited})
            if not audited and not state.get("ignored"):
                return {"summary": "no new lines"}  # an idle poll: the watch loop logs it at DEBUG, not every 5 s
            return {"summary": f"no new signals (audited {audited} lines{ignored_note})"}
        _safely(anomaly.record_errors, len(signal_lines))  # spike detection across all errors
        groups: dict[str, list] = {}
        for ev in signal_lines:  # one signal per distinct error, never one blob per batch
            groups.setdefault(issues.fingerprint(ev["service"] or "", ev["message"]), []).append(ev)
        filed, counted, held_n, new_n = [], 0, 0, 0
        for fp, lines in groups.items():
            signal = build_signal(lines, state.get("quarantine") or [], state.get("source_name", ""))
            signal["fingerprint"] = fp
            if state.get("backlog"):
                signal["backlog"] = True  # old history replayed on purpose, not something that just happened
            signal.update(error_kinds.tag(signal))  # category + details for operational errors (config, rate limit, ...)
            signal.update(severity_rules.apply(signal))  # CRITICAL for configured services / patterns
            held = severity_rules.should_hold(signal)  # quiet hours: store and count, hold the card
            if held:
                signal["held"] = True
            item_id = create_item("signal", signal, status="held" if held else "new")
            bind_trace(item_id)  # one error = one trace across every agent
            issue_id, action = issues.record_occurrence(signal, item_id)
            update_item(item_id, issue_id=issue_id, **({"status": "counted"} if action == "count" else {}))
            if action == "count":  # a known issue: counted on the issue, no new card, no handoff
                counted += 1
                step(logger, "monitor.07.emit", "known issue, occurrence counted only", signal_id=item_id,
                     issue_id=issue_id, error=signal["error"])
                continue
            if _is_new_issue(issue_id):
                new_n += 1
            if held:
                held_n += 1
                step(logger, "monitor.07.emit", "quiet hours: card held until they end", signal_id=item_id,
                     issue_id=issue_id, severity=signal["severity"], error=signal["error"])
                continue
            title = f"signal detected: {signal['service']} {signal['severity']}" + (" (backlog)" if signal.get("backlog") else "")
            body = NL.join(["*ERROR*", signal["error"], "", "*DETAILS*",
                            f"{len(signal['evidence'])} evidence line(s) · {signal['ts']} · signal `{item_id}`"])
            step(logger, "monitor.07.emit", "signal stored in DB", signal_id=item_id, issue_id=issue_id,
                 service=signal["service"], severity=signal["severity"], error=signal["error"],
                 evidence=len(signal["evidence"]), category=signal.get("category"))
            deliveries = notify(title, body)
            if isinstance(deliveries, list):
                update_item(item_id, delivery=deliveries)  # per channel: ok / HTTP status / error
            if signal["severity"] == "CRITICAL":
                paging.page(title, body, "CRITICAL")
            step(logger, "monitor.08.notify", "signal notification sent", signal_id=item_id)
            logger.info("signal.filed", extra={"signal_id": item_id, "service": signal["service"],
                                               "severity": signal["severity"], "error": signal["error"],
                                               "evidence": len(signal["evidence"]),
                                               "source": state.get("source_name", "")})
            filed.append((item_id, signal))
        _safely(anomaly.record_new_issues, new_n)  # several new kinds at once = something changed
        if not filed:
            held_note = f", {held_n} held for quiet hours" if held_n else ""
            return {"summary": f"no new signals ({counted} known issue occurrence(s) counted{held_note}, "
                               f"audited {audited} lines{ignored_note})"}
        first_id, first = filed[0]
        more = f" (+{len(filed) - 1} more: {', '.join(i for i, _ in filed[1:])})" if len(filed) > 1 else ""
        return {
            "signal_id": first_id,
            "signal_ids": [i for i, _ in filed],
            "summary": (
                f"signal {first_id}: {first['service']} {first['severity']} "
                f"{first['error']} ({len(first['evidence'])} evidence lines){more}"
            ),
        }

    g = StateGraph(State)
    g.add_node("collect", collect)
    g.add_node("classify", classify_node)
    g.add_node("emit", emit)
    g.add_edge(START, "collect")
    g.add_edge("collect", "classify")
    g.add_edge("classify", "emit")
    g.add_edge("emit", END)
    return g.compile()


def _safely(fn, *args) -> None:
    """Volume/anomaly bookkeeping must never break the monitor."""
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001
        step(logger, "monitor.07.emit", "bookkeeping failed", level="error", fn=getattr(fn, "__name__", "?"),
             error=f"{type(e).__name__}: {e}"[:200])


def _is_new_issue(issue_id: str) -> bool:
    issue = issues.get_issue(issue_id)
    return bool(issue and issue["data"].get("count") == 1)


def has_dynamic_parts(template: str) -> bool:
    """True when the path changes by itself: a {date} placeholder or a wildcard."""
    return "{date}" in template or any(c in template for c in "*?[")


def _open_source(source: str, service: str | None = None, start_at_end: bool = False):
    """(reader, label, graph, fresh): resolves the source, loads the saved cursor, compiles the pipeline.

    `fresh` is True when this log was never watched before. With `start_at_end` such a log is read from its END:
    a live tail reports what happens from now on, not years of history as hundreds of incidents."""
    fresh = False
    reader = get_source(expand_path(source))
    label = expand_path(source).strip() or "default"
    step(logger, "monitor.02.source", "log source resolved", reader=type(reader).__name__)
    if isinstance(reader, LocalFileReader):
        # Resume where the last run stopped (cursor survives restarts).
        label = str(reader.path)
        exists = reader.path.exists()
        step(logger, "monitor.02.source", "checking log file path",
             level="info" if exists else "error", path=label, exists=exists,
             size=reader.path.stat().st_size if exists else None)
        saved = _load_offset(label)
        fresh = saved is None
        if saved is not None:
            reader.offset = saved
        elif start_at_end and exists:
            reader.offset = reader.path.stat().st_size
            _save_offset(label, reader.offset)
            step(logger, "monitor.03.cursor", "new log: starting at its end, old errors are not replayed",
                 path=label, offset=reader.offset)
        step(logger, "monitor.03.cursor", "cursor loaded", path=label,
             saved_offset=saved, resuming=saved is not None)
    return reader, label, build(reader, service), fresh


def _save(reader, label: str) -> None:
    if isinstance(reader, LocalFileReader) and reader.offset is not None:
        _save_offset(label, reader.offset)


def run(source: str = "", mode: str = "once", poll_seconds: int = 0, on_signal=None) -> str:
    """Tail the source and file signals (one signal per distinct error).

    Args:
        source: '' (env MONITOR_SOURCE default) | 'local_file' | 'ssh' | a local file path. A path may
            contain {date} (today's YYYY-MM-DD) or a wildcard (the newest match) and is followed across
            midnight in watch mode. A single never-renamed file needs neither: truncation and rename rotation
            are detected.
        mode: 'once' (single pass, for tests/cron) | 'watch' (poll forever).
        poll_seconds: 0 = settings.poll_seconds.
        on_signal: optional callback(signal_id) fired in watch mode for every NEW (not repeat) signal
            (used by sre.auto to hand off to the supervisor).
    """
    step(logger, "monitor.01.start", "monitor run started", source=(source or "").strip() or "default", mode=mode)
    full_name = source  # watcher health is keyed by the name exactly as configured
    source, service = split_source(source)  # "path@service": every incident from this source carries that service
    replay = os.getenv("MONITOR_REPLAY_BACKLOG", "false").strip().lower() == "true"
    reader, label, graph, fresh = _open_source(source, service, start_at_end=(mode == "watch" and not replay))
    backlog = bool(fresh and replay and mode == "watch")  # the first pass replays history: say so on its signals
    wait = poll_seconds or settings.poll_seconds
    try:
        if mode == "watch":
            fired, checks = 0, 0
            try:
                while True:
                    current = expand_path(source).strip()
                    if (has_dynamic_parts(source) and str(Path(current)) != label
                            and isinstance(reader, LocalFileReader)):
                        step(logger, "monitor.02.source", "log file rolled over, following the new file",
                             old=label, new=current)
                        reader.close()
                        reader, label, graph, _ = _open_source(source, service)  # a new file is read from its start
                    out = graph.invoke({"text": None, "source_name": label, "backlog": backlog})
                    backlog = False
                    checks += 1
                    _save(reader, label)  # persist before handing off
                    for signal_id in out.get("signal_ids") or []:
                        fired += 1
                        if on_signal is not None:
                            on_signal(signal_id)
                    clear_trace()  # the hand-off agents ran in this thread: do not tag the heartbeat with their trace
                    step(logger, "monitor.09.watch", "poll complete", check=checks, signals=fired,
                         level="debug" if out.get("summary") == "no new lines" else "info",
                         result=out.get("summary", ""), next_poll_s=wait)
                    _safely(health.report, f"watcher:{full_name}", True, f"Log watcher recovered: {label}")
                    time.sleep(wait)
            except KeyboardInterrupt:
                return f"watching {label}: {checks} checks, {fired} signals (stopped)"
        out = graph.invoke({"text": None, "source_name": label, "backlog": backlog})
        _save(reader, label)
        return out.get("summary", "done")
    finally:
        reader.close()

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
import glob
import os
import time
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from sre_agent.core.config import settings
from sre_agent.core.db import create_item, list_items, update_item
from sre_agent.sre import issues
from sre_agent.core.notify import notify
from sre_agent.core.sre_log import bind_trace, clear_trace, get_logger, step

logger = get_logger("monitor")

from .local_file import LocalFileReader
from .log_source import get_source
from .parse import build_signal, classify

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


def build(reader=None):
    """Compile the monitor graph. reader is the live LogSource (closed over).

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
        step(logger, "monitor.04.collect", "read new log text", source=src,
             chars=len(text), offset=getattr(reader, "offset", None))
        return {"text": text}

    def classify_node(state: State) -> State:
        text = (state.get("text") or "").strip()
        if not text:
            step(logger, "monitor.05.classify", "no new lines to classify")
            return {
                "signal_lines": [],
                "audit_lines": [],
                "quarantine": [],
                "summary": "no new lines",
            }
        parts = classify(text.splitlines())
        step(logger, "monitor.05.classify", "classified lines",
             lines=len(text.splitlines()), signal=len(parts["signal"]),
             audit=len(parts["audit"]), quarantine=len(parts["quarantine"]))
        for q in parts["quarantine"]:
            step(logger, "monitor.05.classify", "unparsable line quarantined", level="warning",
                 raw=q["raw"][:200])
        return {
            "signal_lines": parts["signal"],
            "audit_lines": parts["audit"],
            "quarantine": parts["quarantine"],
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
        if not signal_lines:
            step(logger, "monitor.07.emit", "no signal to file", audited=audited)
            logger.debug("signal.none", extra={"source": state.get("source_name", ""), "audited": audited})
            return {"summary": f"no new signals (audited {audited} lines)"}
        groups: dict[str, list] = {}
        for ev in signal_lines:  # one signal per distinct error, never one blob per batch
            groups.setdefault(issues.fingerprint(ev["service"] or "", ev["message"]), []).append(ev)
        filed, counted = [], 0
        for fp, lines in groups.items():
            signal = build_signal(lines, state.get("quarantine") or [], state.get("source_name", ""))
            signal["fingerprint"] = fp
            item_id = create_item("signal", signal)
            bind_trace(item_id)  # one error = one trace across every agent
            issue_id, action = issues.record_occurrence(signal, item_id)
            update_item(item_id, issue_id=issue_id)
            if action == "count":  # a known issue: counted on the issue, no new card, no handoff
                counted += 1
                step(logger, "monitor.07.emit", "known issue, occurrence counted only", signal_id=item_id,
                     issue_id=issue_id, error=signal["error"])
                continue
            title = f"signal detected: {signal['service']} {signal['severity']}"
            body = NL.join([f"error: {signal['error']}", f"evidence lines: {len(signal['evidence'])}",
                            f"ts: {signal['ts']}", f"signal id: {item_id}"])
            step(logger, "monitor.07.emit", "signal stored in DB", signal_id=item_id, issue_id=issue_id,
                 service=signal["service"], severity=signal["severity"], error=signal["error"],
                 evidence=len(signal["evidence"]))
            notify(title, body)
            step(logger, "monitor.08.notify", "signal notification sent", signal_id=item_id)
            logger.info("signal.filed", extra={"signal_id": item_id, "service": signal["service"],
                                               "severity": signal["severity"], "error": signal["error"],
                                               "evidence": len(signal["evidence"]),
                                               "source": state.get("source_name", "")})
            filed.append((item_id, signal))
        if not filed:
            return {"summary": f"no new signals ({counted} known issue occurrence(s) counted, "
                               f"audited {audited} lines)"}
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


def has_dynamic_parts(template: str) -> bool:
    """True when the path changes by itself: a {date} placeholder or a wildcard."""
    return "{date}" in template or any(c in template for c in "*?[")


def expand_path(template: str, now: datetime | None = None) -> str:
    """Resolve a rotating log path: {date} -> YYYY-MM-DD, and a wildcard (errors-*.jsonl) -> the newest match.

    Compressed (.gz) files are skipped and symlinks are resolved, so errors-current.jsonl and the dated file
    it points to count as one file. With no match the pattern is returned, so the "file not found" error names it.
    """
    path = template.replace("{date}", (now or datetime.now()).strftime("%Y-%m-%d"))
    if any(c in path for c in "*?["):
        found = [m for m in glob.glob(path) if os.path.isfile(m) and not m.endswith(".gz")]
        if found:
            return os.path.realpath(max(found, key=os.path.getmtime))
    return path


def _open_source(source: str):
    """(reader, label, graph): resolves the source, loads the saved cursor, compiles the pipeline."""
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
        if saved is not None:
            reader.offset = saved
        step(logger, "monitor.03.cursor", "cursor loaded", path=label,
             saved_offset=saved, resuming=saved is not None)
    return reader, label, build(reader)


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
    reader, label, graph = _open_source(source)
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
                        reader, label, graph = _open_source(source)
                    out = graph.invoke({"text": None, "source_name": label})
                    checks += 1
                    _save(reader, label)  # persist before handing off
                    for signal_id in out.get("signal_ids") or []:
                        fired += 1
                        if on_signal is not None:
                            on_signal(signal_id)
                    clear_trace()  # the hand-off agents ran in this thread: do not tag the heartbeat with their trace
                    step(logger, "monitor.09.watch", "poll complete", check=checks, signals=fired,
                         result=out.get("summary", ""), next_poll_s=wait)
                    time.sleep(wait)
            except KeyboardInterrupt:
                return f"watching {label}: {checks} checks, {fired} signals (stopped)"
        out = graph.invoke({"text": None, "source_name": label})
        _save(reader, label)
        return out.get("summary", "done")
    finally:
        reader.close()

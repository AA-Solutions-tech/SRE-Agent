"""Log line parsing and severity classification (pure Python, no LLM).

Severity policy (locked): ERROR/WARN become signals, INFO/DEBUG are audit
only. Severity is computed here in code — never by the LLM.

Expected line format (Khaleel to confirm against real logs):
    [2026-10-09T11:00:01Z] [ERROR] [payment-api] MongoNetworkTimeout after 3 retries

Lines that do not parse are quarantined: kept verbatim, attached to the
signal record as context when a signal fires, but NEVER paging on their own.
"""

import json
import os
import re
from datetime import datetime, timezone

from sre_agent.core.redact import redact

LINE_RE = re.compile(
    r"^\s*\[(?P<ts>[^\]]+)\]\s+"
    r"\[(?P<level>[^\]]+)\]\s+"
    r"\[(?P<service>[^\]]+)\]\s*"
    r"(?P<message>.*)$"
)

# Second format (Wave node logger, no service in the line):
#     2026-10-19 13:18:04:184 [error] Error fetching report metrics: fetch failed
NODE_LINE_RE = re.compile(
    r"^\s*(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d{1,6})\s+"
    r"\[(?P<level>[A-Za-z]+)\]\s*(?P<message>.*)$"
)
DEFAULT_SERVICE = os.getenv("MONITOR_SERVICE", "wave-node")

# Level -> signal severity. Anything else parsed is audit-only.
LEVEL_SEVERITY = {"ERROR": "HIGH", "WARN": "MEDIUM", "WARNING": "MEDIUM"}
AUDIT_LEVELS = {"INFO", "DEBUG"}

_ERROR_NAME_RE = re.compile(r"\w+")

# JSON-lines loggers (one object per line: timestamp, level, message, service ...).
_JSON_LEVEL_ALIASES = {"FATAL": "ERROR", "CRITICAL": "ERROR", "ERR": "ERROR", "WARNING": "WARN"}


def _status(obj: dict) -> int:
    """HTTP status of an access record (0 when missing or not a number)."""
    try:
        return int(obj.get("statusCode") or 0)
    except (TypeError, ValueError):
        return 0


def _parse_json_line(line: str) -> dict | None:
    """Parse one JSON-lines record, or None when the line is not a JSON log object."""
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not obj.get("level") or not obj.get("message"):
        return None
    level = str(obj["level"]).strip().upper()
    level = _JSON_LEVEL_ALIASES.get(level, level)
    # Request-access lines (kind=access) with a client status (<500) are normal traffic, not faults:
    # keep them as audit so a 400/403 never pages. 5xx access lines still count.
    if obj.get("kind") == "access" and level == "WARN" and _status(obj) < 500:
        level = "INFO"
    return {
        "ts": str(obj.get("timestamp") or obj.get("time") or "") or None,
        "level": level,
        "service": str(obj.get("service") or os.getenv("MONITOR_SERVICE", DEFAULT_SERVICE)),
        "message": str(obj["message"]).strip(),
        "raw": line.rstrip("\n"),
        "parsed": True,
    }


def parse_line(line: str) -> dict:
    """Parse one log line. Never raises; unparsable lines come back raw."""
    as_json = _parse_json_line(line)
    if as_json:
        return as_json
    m = LINE_RE.match(line)
    if not m:
        n = NODE_LINE_RE.match(line)
        if n:
            return {
                "ts": n.group("ts"),
                "level": n.group("level").upper(),
                "service": os.getenv("MONITOR_SERVICE", DEFAULT_SERVICE),
                "message": n.group("message").strip(),
                "raw": line.rstrip("\n"),
                "parsed": True,
            }
    if not m:
        return {
            "ts": None,
            "level": None,
            "service": None,
            "message": line.strip(),
            "raw": line.rstrip("\n"),
            "parsed": False,
        }
    return {
        "ts": m.group("ts").strip() or None,
        "level": m.group("level").strip().upper(),
        "service": m.group("service").strip() or None,
        "message": m.group("message").strip(),
        "raw": line.rstrip("\n"),
        "parsed": True,
    }


def classify(lines: list[str], service: str | None = None) -> dict:
    """Split lines into signal (page), audit (quiet), quarantine (unparseable).

    Returns {'signal': [...], 'audit': [...], 'quarantine': [...]} where each
    entry is a parse_line() dict. Blank lines are ignored entirely.
    `service` (from a "path@service" source) names every parsed event, whatever the log line says.
    """
    signal, audit, quarantine = [], [], []
    for line in lines:
        if not line.strip():
            continue
        ev = parse_line(line)
        if service and ev["parsed"]:
            ev["service"] = service
        if not ev["parsed"] or ev["level"] not in (*LEVEL_SEVERITY, *AUDIT_LEVELS):
            quarantine.append(ev)
        elif ev["level"] in LEVEL_SEVERITY:
            signal.append(ev)
        else:
            audit.append(ev)
    return {"signal": signal, "audit": audit, "quarantine": quarantine}


def _error_name(message: str) -> str:
    """Short error name: the text before the first ':' (ids stripped) when it reads like a title,
    else the leading word token, else the message truncated."""
    msg = message.strip()
    if ":" in msg:
        head = re.sub(r"\s+for\s+[0-9a-fA-F]{12,}.*$|[0-9a-fA-F]{12,}", "", msg.split(":")[0]).strip()
        if len(head) >= 8 and head.lower() not in ("error", "failed", "exception"):
            return head[:80]
    m = _ERROR_NAME_RE.match(msg)
    if m:
        return m.group(0)[:80]
    return msg[:80] or "unknown"


def build_signal(signal_lines: list[dict], quarantine: list[dict], source: str) -> dict:
    """Build the signal record the investigate agent consumes.

    Contract (frozen — investigate agent depends on these keys):
      service, severity, error, evidence[], ts, source (+ quarantine[] context).
    """
    first_error = next(
        (e for e in signal_lines if e["level"] == "ERROR"), signal_lines[0]
    )
    severity = "HIGH" if any(e["level"] == "ERROR" for e in signal_lines) else "MEDIUM"
    ts = next(
        (e["ts"] for e in reversed(signal_lines) if e["ts"]),
        datetime.now(timezone.utc).isoformat(),
    )
    # Secrets are masked BEFORE the record is stored: the database, the API and every prompt then never hold them.
    record = {
        "service": first_error["service"] or "unknown",
        "severity": severity,
        "error": redact(_error_name(first_error["message"])),
        "evidence": [redact(e["raw"]) for e in signal_lines],
        "ts": ts,
        "source": source,
    }
    if quarantine:
        record["quarantine"] = [redact(e["raw"]) for e in quarantine]
    return record

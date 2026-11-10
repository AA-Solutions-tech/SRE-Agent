"""Shared logging for every SRE agent (standard library `logging`).

One JSON object per line in a rotating file under data/logs/ (gitignored),
so the whole loop is tail-able: monitor filed what, supervisor routed
where, incident gated what. Console mirror is opt-in.

Usage at call sites::

    from sre_agent.core.sre_log import get_logger
    logger = get_logger("monitor")          # module top, once
    logger.info("signal.filed", extra={"signal_id": item_id, ...})
    logger.warning("route.fallback", extra={"reason": str(e)})
    logger.error("delegate.failed", extra={"target": t})

Three levels carry the meaning: INFO = normal flow, WARNING = degraded but
continuing (fallbacks, rejected gates, channel failures), ERROR = failed
with no automatic recovery. DEBUG exists for quiet/audit lines only.

Env:
    SRE_LOG_PATH     default $SRE_HOME/logs/sre.log
    SRE_LOG_LEVEL    default INFO
    SRE_LOG_CONSOLE  default false (true = also print lines to stdout)
    SRE_LOG_MAX_MB / SRE_LOG_BACKUPS  rotation, defaults 5 MB x 3

Secrets never reach the log: RedactFilter masks secret keys and token=
query values on every record, no matter the call site.
"""
import contextvars
import json
import logging
import os
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from .env import home_path

REDACT_SUBSTRINGS = ("token", "password", "secret", "authorization", "webhook", "api_key", "apikey")
_TOKEN_PARAM_RE = re.compile(r"(token=)[^&\s]+", re.IGNORECASE)
_CONFIGURED = False


def _redact(obj):
    if isinstance(obj, dict):
        return {
            k: ("***" if any(s in str(k).lower() for s in REDACT_SUBSTRINGS) else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    if isinstance(obj, str):
        return _TOKEN_PARAM_RE.sub(r"\1***", obj)
    return obj


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as one JSON line (event = the message)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "logger": record.name,
            "level": record.levelname,
            "event": record.getMessage(),
        }
        extra = {k: v for k, v in record.__dict__.items() if k not in (
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "taskName", "message", "asctime")}
        if extra:
            payload["details"] = _redact(extra)
        return json.dumps(payload, default=str)


class RedactFilter(logging.Filter):
    """Mask secret values in the message itself (extra is redacted by JsonFormatter)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _TOKEN_PARAM_RE.sub(r"\1***", record.msg)
        return True


def configure_logging(path: str | None = None, level: str | None = None,
                      console: bool | None = None) -> logging.Logger:
    """Configure the shared `sre` file logger once (idempotent)."""
    global _CONFIGURED
    root = logging.getLogger("sre")
    if _CONFIGURED and root.handlers:
        return root
    log_path = path or str(home_path("SRE_LOG_PATH", "logs/sre.log"))
    log_level = (level or os.getenv("SRE_LOG_LEVEL", "INFO")).upper()
    show_console = console if console is not None else os.getenv("SRE_LOG_CONSOLE", "false").lower() == "true"
    try:
        max_bytes = int(float(os.getenv("SRE_LOG_MAX_MB", "5")) * 1024 * 1024)
    except ValueError:
        max_bytes = 5 * 1024 * 1024
    try:
        backups = int(os.getenv("SRE_LOG_BACKUPS", "3"))
    except ValueError:
        backups = 3
    root.setLevel(getattr(logging, log_level, logging.INFO))
    root.propagate = False
    if not root.handlers:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=backups)
        fh.setFormatter(JsonFormatter())
        fh.addFilter(RedactFilter())
        root.addHandler(fh)
        if show_console:
            ch = logging.StreamHandler()
            ch.setFormatter(JsonFormatter())
            root.addHandler(ch)
    _CONFIGURED = True
    return root


def get_logger(name: str) -> logging.Logger:
    """Return the `sre.<name>` logger, configuring the file handler on first use."""
    configure_logging()
    return logging.getLogger(f"sre.{name}")


_trace: contextvars.ContextVar[str] = contextvars.ContextVar("sre_trace", default="")


def bind_trace(trace_id: str | None) -> None:
    """Tag every following step() line of this thread/task with a trace id (the signal id of one error).

    Agents call it at the start of each node, so one error can be followed across every agent with one search."""
    if trace_id:
        _trace.set(str(trace_id))


def clear_trace() -> None:
    """Forget the trace (a long-running loop calls this between errors so old ids never tag new lines)."""
    _trace.set("")


def current_trace() -> str:
    return _trace.get()


def step(logger: logging.Logger, step_id: str, message: str, level: str = "info", **fields) -> None:
    """Log one pipeline step to the JSON log AND print a readable line to the console.

        step(logger, "monitor.03.classify", "classified lines", signal=2, audit=5)
        -> console: 14:02:11 INFO  [monitor.03.classify] classified lines | signal=2 audit=5

    step_id = <agent>.<NN>.<node> so every line says which agent and graph node produced it.
    Console printing is on by default; set SRE_TRACE=false to silence it (file log stays).
    """
    lvl = getattr(logging, level.upper(), logging.INFO)
    trace = fields.get("trace") or current_trace()
    logger.log(lvl, message, extra={"step": step_id, **({"trace": trace} if trace else {}), **fields})
    if lvl == logging.DEBUG and os.getenv("SRE_TRACE_DEBUG", "false").lower() != "true":
        return  # DEBUG goes to the log only when the log level allows it; it is never printed to the console
    if os.getenv("SRE_TRACE", "true").lower() != "false":
        kv = " ".join(f"{k}={_redact({k: v})[k]}" for k, v in fields.items() if k != "trace")
        ts = datetime.now().strftime("%H:%M:%S")
        tag = f" [trace {trace}]" if trace else ""
        print(f"{ts} {level.upper():<5} [{step_id}]{tag} {message}" + (f" | {kv}" if kv else ""), flush=True)

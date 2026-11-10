"""Pure helpers for log source names and for explaining why one cannot be read. No heavy imports: the doctor uses them.

A source may carry the service name it belongs to, after an @:

    MONITOR_LOG_PATH=/var/log/qa/error.log@qa-api, /var/log/prod/error.log@prod-api, docker:web@storefront

Every incident from that source then carries that service name (even when the app's own log lines name another),
so QA and production cards are never mixed up.
"""
import glob
import os
import re
from datetime import datetime
from pathlib import Path

_SERVICE_RE = re.compile(r"^[\w.\-]+$")


def split_source(source: str | None) -> tuple[str, str | None]:
    """("path or name", service or None) from "path@service". An @ only counts when what follows it looks like a
    service name (no slash), so a path that happens to contain @ is left alone."""
    text = (source or "").strip()
    head, sep, tail = text.rpartition("@")
    if sep and head.strip() and _SERVICE_RE.match(tail):
        return head.strip(), tail
    return text, None


def parse_sources(raw: str | None) -> list[tuple[str, str | None]]:
    """[(source, service)] from a comma separated MONITOR_LOG_PATH."""
    return [split_source(part) for part in (raw or "").split(",") if part.strip()]


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


def is_file_source(source: str) -> bool:
    key = (source or "").strip()
    return bool(key) and key not in ("ssh", "local_file") and not key.startswith(("docker:", "journald:"))


def readable_problem(path: str) -> str | None:
    """None when `path` is a readable file for this user; otherwise what is wrong and the exact commands to fix it.

    The packaged service runs with ProtectHome=true, which hides /home completely, so a log under a home directory
    needs a drop-in and an ACL; both are named here instead of leaving the agent silently watching nothing."""
    p = Path(path)
    if p.is_file() and os.access(p, os.R_OK):
        return None
    posix = str(p).replace("\\", "/")
    under_home = posix.startswith("/home/")
    commands = []
    if under_home:
        home = "/".join(posix.split("/")[:3])
        commands += ["sudo systemctl edit sre-agent   (add: [Service] then ProtectHome=read-only)",
                     f"sudo setfacl -m u:sre-agent:x {home}"]
    commands.append(f"sudo setfacl -R -m u:sre-agent:rX {posix.rsplit('/', 1)[0] or '/'}")
    if not p.exists():
        why = "does not exist" + (" or is hidden from the service (ProtectHome hides /home)" if under_home else
                                  " yet (the agent waits for it)")
    elif p.is_dir():
        why = "is a directory, not a log file"
    else:
        why = "exists but cannot be read by this user"
    return f"{path} {why}. Fix: " + "; ".join(commands)


def hint_for(source: str, error: Exception) -> str:
    """Extra guidance to append to a watcher failure."""
    if isinstance(error, (PermissionError, FileNotFoundError)):
        path = split_source(source)[0]
        if is_file_source(path):
            return " " + (readable_problem(path) or "")
    return ""

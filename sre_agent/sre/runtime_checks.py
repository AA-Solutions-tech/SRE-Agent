"""Runtime checks the incident agent can run without touching anything.

  env_status(name)   is the variable set / empty / placeholder / missing in the app's env file(s)?
  tcp_check(h, p)    does host:port accept connections?

Safety: env values are never returned or logged (only set / empty / placeholder / missing and key NAMES).
TCP checks only go to loopback / private addresses, or hosts listed in RUNTIME_CHECK_HOSTS, so a hostile
log line cannot make the agent probe arbitrary machines.
"""
import difflib
import ipaddress
import os
import re
import socket
from pathlib import Path

from sre_agent.core.config import settings

TCP_TIMEOUT = 3.0
MAX_TCP_CHECKS = 3
_PLACEHOLDER = re.compile(r"^(?:change[-_ ]?me|your[-_ ].*|xxx+|<.*>|todo|placeholder|replace.*|\*+)$", re.I)


def env_files() -> list[Path]:
    """APP_ENV_FILES (comma separated), else REPO_DIR/.env when it exists."""
    raw = [p.strip() for p in os.getenv("APP_ENV_FILES", "").split(",") if p.strip()]
    if raw:
        return [Path(p).expanduser() for p in raw]
    default = Path(settings.repo_dir) / ".env"
    return [default] if default.is_file() else []


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _state(value: str) -> str:
    if not value:
        return "empty"
    return "placeholder" if _PLACEHOLDER.match(value) else "set"


def env_status(name: str) -> dict:
    """{"variable", "checked", "sources": [{"source", "state"}], "similar": [names]} (no values)."""
    sources, similar = [], []
    for path in env_files():
        try:
            values = _parse_env(path)
        except OSError as e:
            sources.append({"source": str(path), "state": f"unreadable ({type(e).__name__})"})
            continue
        if name in values:
            sources.append({"source": str(path), "state": _state(values[name])})
        else:
            sources.append({"source": str(path), "state": "missing"})
            similar += difflib.get_close_matches(name, list(values), n=3, cutoff=0.6)
    return {"variable": name, "checked": bool(sources), "sources": sources,
            "similar": list(dict.fromkeys(similar))}


def host_allowed(host: str) -> bool:
    extra = {h.strip().lower() for h in os.getenv("RUNTIME_CHECK_HOSTS", "").split(",") if h.strip()}
    host = host.lower()
    if host == "localhost" or host in extra:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def tcp_check(host: str, port: int, timeout: float = TCP_TIMEOUT) -> dict:
    """{"host", "port", "reachable", "error"?}; "skipped" when the host is not on the allowed list."""
    if not host_allowed(host):
        return {"host": host, "port": port, "reachable": None, "error": "skipped (not a private address; "
                "add it to RUNTIME_CHECK_HOSTS to allow the check)"}
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return {"host": host, "port": port, "reachable": True}
    except OSError as e:
        return {"host": host, "port": port, "reachable": False, "error": type(e).__name__}


def tcp_checks(hosts: list) -> list[dict]:
    return [tcp_check(h, p) for h, p in hosts[:MAX_TCP_CHECKS]]

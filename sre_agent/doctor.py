"""`sre-agent doctor`: check an install before (and while) running it.

--quick runs only the local checks the service needs to start (used by systemd ExecStartPre), so an LLM or Slack
outage never blocks start-up. Every check returns (status, name, detail); any FAIL makes the exit code 1.
"""
import os
import shlex
import shutil
import socket
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

from sre_agent import __version__
from sre_agent.core.config import settings
from sre_agent.core.secret_rules import secret_problem

OK, WARN, FAIL = "OK", "WARN", "FAIL"
LLM_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY",
    "groq": "GROQ_API_KEY", "gemini": "GOOGLE_API_KEY",
}


def check_approval_secret():
    why = secret_problem(settings.approval_secret)
    return (FAIL, "APPROVAL_SECRET", f"{why}; run: sudo sre-agent setup  (or set it to: sre-agent gen-secret)") if why \
        else (OK, "APPROVAL_SECRET", "set")


def check_api_key():
    if not settings.api_key:
        return WARN, "SRE_API_KEY", "empty: /run, /signals, /issues are open to anyone who can reach the port"
    why = secret_problem(settings.api_key)
    return (FAIL, "SRE_API_KEY", why) if why else (OK, "SRE_API_KEY", "set")


def check_writable(name: str, path: Path):
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".doctor-probe"
        probe.write_text("ok")
        probe.unlink()
        return OK, name, str(path)
    except OSError as e:
        return FAIL, name, f"{path} not writable: {e.strerror or e}"


def check_db():
    from sre_agent.core.db import migrate

    try:
        with sqlite3.connect(settings.db_path) as c:
            version = migrate(c)
        return OK, "database", f"{settings.db_path} (schema v{version})"
    except (sqlite3.Error, RuntimeError) as e:
        return FAIL, "database", f"{settings.db_path}: {e}"


def check_bind():
    host = "0.0.0.0" if settings.bind in ("0.0.0.0", "::") else settings.bind
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, settings.port))
        return OK, "listen address", f"{settings.bind}:{settings.port} is free"
    except OSError as e:
        return WARN, "listen address", f"{settings.bind}:{settings.port} not bindable now ({e.strerror}); in use?"


def check_git():
    path = shutil.which("git")
    return (OK, "git", path) if path else (FAIL, "git", "git not found on PATH (needed for fixes and PRs)")


def check_command(env_key: str):
    cmd = os.getenv(env_key, "").strip()
    if not cmd:
        level = WARN if env_key == "TEST_CMD" else OK
        return level, env_key, "not set" + (": fixes are not tested before a PR" if level == WARN else "")
    try:
        exe = shlex.split(cmd, posix=os.name != "nt")[0]
    except (ValueError, IndexError):
        return FAIL, env_key, "cannot parse the command"
    found = shutil.which(exe)
    return (OK, env_key, f"{exe} -> {found}") if found else (WARN, env_key, f"'{exe}' not on PATH for this user")


def check_llm_key():
    provider = settings.llm_provider
    key = LLM_KEYS.get(provider)
    if key is None:
        return OK, "LLM key", f"provider {provider}: no key check"
    return (OK, "LLM key", f"{key} set") if os.getenv(key) else \
        (WARN, "LLM key", f"{key} empty: agents fall back to offline heuristics")


def check_notify():
    if settings.slack_webhook or settings.gchat_webhook or (settings.smtp_user and settings.email_to):
        return OK, "notifications", ", ".join(settings.notify_channels)
    return WARN, "notifications", "no Slack/GChat webhook or SMTP set: nobody will see incidents"


def _loopback(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host in ("localhost", "0.0.0.0", "::1") or host.startswith("127.")


def check_app_base_url():
    """Approve / Reject / Ignore links open APP_BASE_URL. A loopback address means every button opens the reader's
    own computer, so no decision can ever be recorded (the pilot recorded none)."""
    from sre_agent.sre import slack_app

    url = settings.app_base_url
    if not _loopback(url):
        return (OK, "APP_BASE_URL", url) if url.startswith("https://") else \
            (WARN, "APP_BASE_URL", f"{url} is plain http: put the service behind HTTPS before anyone clicks links")
    if slack_app.configured() and "slack" in settings.notify_channels and \
            not ({"email", "gchat"} & set(settings.notify_channels)):
        return OK, "APP_BASE_URL", f"{url} is loopback, but Slack buttons decide inside Slack (interactivity is configured)"
    return (WARN, "APP_BASE_URL", f"{url} is a loopback address: buttons opened from Slack or email will not work. "
            "Set the public HTTPS URL, or configure the Slack app (SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET and "
            "its Interactivity URL) so Approve/Reject/Ignore work inside Slack")


def check_log_sources():
    """Can the service read every log file it is meant to watch? Names the exact fix when it cannot (the packaged
    unit hides /home with ProtectHome=true, and then the agent starts but watches nothing)."""
    from sre_agent.sre.monitor.sources import expand_path, is_file_source, parse_sources, readable_problem

    if os.getenv("MONITOR_SOURCE", "local_file").strip() == "ssh" and not os.getenv("MONITOR_LOG_PATH", "").strip():
        return OK, "log sources", "remote log over SSH"
    entries = parse_sources(os.getenv("MONITOR_LOG_PATH", ""))
    if not entries:
        return WARN, "log sources", "MONITOR_LOG_PATH is empty: nothing is being watched"
    files = [src for src, _ in entries if is_file_source(src)]
    problems = [p for p in (readable_problem(expand_path(src)) for src in files) if p]
    if problems:
        return WARN, "log sources", " | ".join(problems)
    return OK, "log sources", f"{len(entries)} source(s), {len(files)} file(s) readable"


def check_llm_live():
    """One tiny real model call (full doctor only): a wrong key, model name or rejected parameter shows up here."""
    from sre_agent.sre import llm_check

    if not llm_check.configured():
        return OK, "LLM call", "skipped (no key configured)"
    ok, why = llm_check.selftest()
    return (OK, "LLM call", f"{settings.llm_provider} answered") if ok else (FAIL, "LLM call", why)


def check_ssh():
    if os.getenv("MONITOR_SOURCE", "local_file").strip() != "ssh":
        return OK, "SSH log source", "not used"
    problems = [n for n, p in (("key", settings.ssh_key_path), ("known_hosts", settings.ssh_known_hosts))
                if not os.access(p, os.R_OK)]
    if not settings.ssh_host:
        return FAIL, "SSH log source", "MONITOR_SOURCE=ssh but SSH_HOST is empty"
    if problems:
        return FAIL, "SSH log source", f"cannot read {', '.join(problems)} ({settings.ssh_key_path})"
    return OK, "SSH log source", f"{settings.ssh_user}@{settings.ssh_host}:{settings.ssh_port}"


def check_ssh_reachable():
    if os.getenv("MONITOR_SOURCE", "local_file").strip() != "ssh" or not settings.ssh_host:
        return OK, "SSH reachable", "not used"
    try:
        socket.create_connection((settings.ssh_host, settings.ssh_port), timeout=5).close()
        return OK, "SSH reachable", f"{settings.ssh_host}:{settings.ssh_port}"
    except OSError as e:
        return FAIL, "SSH reachable", f"{settings.ssh_host}:{settings.ssh_port}: {e}"


def run_checks(quick: bool = False) -> list[tuple[str, str, str]]:
    checks = [
        lambda: (OK, "version", f"sre-agent {__version__}, Python {sys.version.split()[0]}"),
        check_approval_secret,
        check_api_key,
        lambda: check_writable("SRE_HOME", settings.data_dir),
        check_db,
        check_app_base_url,
        check_log_sources,
    ]
    if not quick:
        checks += [check_bind, check_git, lambda: check_command("TEST_CMD"), lambda: check_command("LINT_CMD"),
                   check_llm_key, check_llm_live, check_notify, check_ssh, check_ssh_reachable]
    results = []
    for check in checks:
        try:
            results.append(check())
        except Exception as e:  # noqa: BLE001  a broken check must not hide the others
            results.append((FAIL, getattr(check, "__name__", "check"), f"{type(e).__name__}: {e}"))
    return results


def main(quick: bool = False, as_json: bool = False) -> int:
    results = run_checks(quick)
    if as_json:
        import json
        print(json.dumps([{"status": s, "check": n, "detail": d} for s, n, d in results], indent=2))
    else:
        width = max(len(n) for _, n, _ in results)
        for status, name, detail in results:
            print(f"[{status:<4}] {name:<{width}}  {detail}")
    failed = sum(1 for s, _, _ in results if s == FAIL)
    if not as_json:
        print(f"\n{failed} failed" if failed else "\nall required checks passed")
    return 1 if failed else 0

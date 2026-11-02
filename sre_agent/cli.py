"""`sre-agent` command line (also `python -m sre_agent`).

    sre-agent serve [--bind 127.0.0.1] [--port 8000]   run the API + auto loop (what the service runs)
    sre-agent run <agent> [args] [--key value]         run one agent once      (sre-agent run --list)
    sre-agent setup [--only llm,notifications,...]     answer questions to create/update the config file
    sre-agent doctor [--quick] [--json]                check config, paths and dependencies
    sre-agent paths                                     show where config, data and logs live
    sre-agent gen-secret                                print a random 64-hex-char secret
    sre-agent version
"""
import argparse
import json
import os
import secrets
import sys
from pathlib import Path

from sre_agent import __version__

LOCK_NAME = "sre-agent.lock"


def _coerce(v: str):
    if v.lstrip("-").isdigit():
        return int(v)
    if v[:1] in "{[":
        try:
            return json.loads(v)
        except ValueError:
            pass
    return v


def parse(argv: list[str]) -> tuple[list, dict]:
    """Positional args stay positional; `--key value` pairs become kwargs (dashes -> underscores)."""
    args, kwargs, i = [], {}, 0
    while i < len(argv):
        if argv[i].startswith("--") and i + 1 < len(argv):
            kwargs[argv[i][2:].replace("-", "_")] = _coerce(argv[i + 1])
            i += 2
        else:
            args.append(argv[i])
            i += 1
    return args, kwargs


def cmd_run(argv: list[str]) -> int:
    from sre_agent.agents import agent_names, load_agent

    if not argv or argv[0] == "--list":
        print("agents:", ", ".join(agent_names()) or "(none)")
        return 0
    try:
        mod = load_agent(argv[0])
    except ModuleNotFoundError:
        print(f"unknown agent '{argv[0]}'. available: {', '.join(agent_names())}", file=sys.stderr)
        return 1
    args, kwargs = parse(argv[1:])
    print(mod.run(*args, **kwargs))
    return 0


def _acquire_lock(home: Path):
    """Exclusive lock in SRE_HOME so a manual `serve` never runs beside the service (double-processed signals)."""
    home.mkdir(parents=True, exist_ok=True)
    fh = open(home / LOCK_NAME, "a+")  # noqa: SIM115  held open for the process lifetime (the lock)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh  # keep the handle open for the life of the process


def cmd_serve(bind: str | None, port: int | None) -> int:
    from sre_agent.core.config import settings
    from sre_agent.doctor import secret_problem

    why = secret_problem(settings.approval_secret)
    if why:
        print(f"refusing to start: APPROVAL_SECRET is {why}. Set it in the config file "
              f"(generate one with: sre-agent gen-secret).", file=sys.stderr)
        return 2
    lock = _acquire_lock(settings.data_dir)
    if lock is None:
        print(f"refusing to start: another sre-agent is already running with SRE_HOME={settings.data_dir}",
              file=sys.stderr)
        return 3
    import uvicorn

    uvicorn.run("sre_agent.server:app", host=bind or settings.bind, port=port or settings.port,
                workers=1, proxy_headers=True, log_level=os.getenv("SRE_UVICORN_LOG_LEVEL", "info"))
    return 0


def cmd_paths() -> int:
    from sre_agent.core.config import settings
    from sre_agent.core.sre_log import configure_logging

    log_file = next((getattr(h, "baseFilename", "") for h in configure_logging().handlers), "")
    rows = {
        "config file": os.path.abspath(os.getenv("SRE_ENV_FILE", ".env")),
        "SRE_HOME": str(settings.data_dir),
        "database": settings.db_path,
        "repo clone": str(settings.repo_dir),
        "worktrees": str(settings.worktrees_dir),
        "log file": log_file,
    }
    for k, v in rows.items():
        print(f"{k:<12} {v}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sre-agent", description="SRE agent: monitor, incident, coding, ship.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the HTTP API and the auto loop")
    s.add_argument("--bind", help="listen address (default SRE_BIND or 127.0.0.1)")
    s.add_argument("--port", type=int, help="listen port (default SRE_PORT or 8000)")
    r = sub.add_parser("run", help="run one agent once", add_help=False)
    r.add_argument("rest", nargs=argparse.REMAINDER)
    d = sub.add_parser("doctor", help="check the installation")
    d.add_argument("--quick", action="store_true", help="local checks only (used before service start)")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    w = sub.add_parser("setup", help="interactive configuration (writes the config file)")
    w.add_argument("--only", help="comma separated sections: llm,notifications,logs,web,code,automation")
    w.add_argument("--env-file", help="config file to edit (default: SRE_ENV_FILE or ./.env)")
    sub.add_parser("paths", help="show config, data and log locations")
    sub.add_parser("gen-secret", help="print a random secret for APPROVAL_SECRET / SRE_API_KEY")
    sub.add_parser("version", help="print the version")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "run":  # pass agent args through untouched (they may look like options)
        return cmd_run(argv[1:])
    ns = build_parser().parse_args(argv)
    if ns.cmd == "serve":
        return cmd_serve(ns.bind, ns.port)
    if ns.cmd == "doctor":
        from sre_agent import doctor
        return doctor.main(quick=ns.quick, as_json=ns.json)
    if ns.cmd == "setup":
        from sre_agent import wizard
        env_file = Path(ns.env_file or os.getenv("SRE_ENV_FILE", ".env")).absolute()
        only = [s.strip() for s in ns.only.split(",") if s.strip()] if ns.only else None
        return wizard.run(env_file, only=only)
    if ns.cmd == "paths":
        return cmd_paths()
    if ns.cmd == "gen-secret":
        print(secrets.token_hex(32))
        return 0
    print(f"sre-agent {__version__}")
    return 0

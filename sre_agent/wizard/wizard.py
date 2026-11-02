"""`sre-agent setup`: interactive configuration.

Walks through the sections in steps.STEPS, shows a masked summary, writes the config file (keeping comments and a
.bak copy), then runs `sre-agent doctor` and offers to (re)start the service.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from sre_agent.core.secret_rules import new_secret, secret_problem

from . import envfile, steps
from .prompts import Prompter, WizardAborted, mask

SECRET_KEY = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|WEBHOOK", re.IGNORECASE)
PLACEHOLDER = "__GENERATED__"
GENERATED_SECRETS = ("SRE_API_KEY", "APPROVAL_SECRET")

INTRO = """\
SRE Agent setup
===============
Config file: {path}
  Enter   keep the value in [brackets], or skip an optional question
  -       clear an optional value
  Ctrl+C  quit without saving anything
"""


def _current(path: Path) -> dict[str, str]:
    return {k: ("" if v == PLACEHOLDER else v) for k, v in envfile.read_values(path).items()}


def _secrets(cur: dict, p: Prompter) -> dict:
    out = {}
    for key in GENERATED_SECRETS:
        if secret_problem(cur.get(key, "")):
            out[key] = new_secret()
            p.info(f"{key} was missing or weak: generated a new random value.")
    return out


def _summary(p: Prompter, changes: dict) -> None:
    p.heading(f"{len(changes)} setting(s) will change")
    width = max(len(k) for k in changes)
    for key, value in changes.items():
        shown = mask(value) if SECRET_KEY.search(key) and value else (value or "(empty)")
        p.info(f"{key:<{width}}  {shown}")


def _doctor_command() -> list[str]:
    launcher = shutil.which("sre-agent")  # the packaged launcher drops root and runs as the service user
    return [launcher, "doctor"] if launcher else [sys.executable, "-m", "sre_agent", "doctor"]


def _offer_service_restart(p: Prompter) -> None:
    if not Path("/run/systemd/system").is_dir() or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    active = subprocess.run(["systemctl", "is-active", "--quiet", "sre-agent"], check=False).returncode == 0
    if p.confirm("Restart sre-agent now to apply the changes?" if active else "Start sre-agent now?", default=True):
        subprocess.run(["systemctl", "enable", "--quiet", "sre-agent"], check=False)
        rc = subprocess.run(["systemctl", "restart", "sre-agent"], check=False).returncode
        p.info("Service is running." if rc == 0 else "Service failed to start: journalctl -u sre-agent -n 50")


def _check_writable(path: Path, p: Prompter) -> bool:
    target = path if path.exists() else path.parent
    if os.access(target, os.W_OK):
        return True
    p.write(f"Cannot write {path}. Run it as root:  sudo sre-agent setup")
    return False


def run(env_file: Path, only: list[str] | None = None, prompter: Prompter | None = None,
        after_save: bool = True, step_funcs: dict | None = None) -> int:
    p = prompter or Prompter()
    funcs = step_funcs or steps.STEPS
    selected = only or list(funcs)
    unknown = [s for s in selected if s not in funcs]
    if unknown:
        p.write(f"Unknown section(s): {', '.join(unknown)}. Choose from: {', '.join(funcs)}")
        return 2
    if not _check_writable(env_file, p):
        return 1

    original = _current(env_file)
    working = dict(original)
    p.write(INTRO.format(path=env_file))
    try:
        working.update(_secrets(working, p))
        for name in selected:
            working.update(funcs[name](p, working))
        changes = {k: v for k, v in working.items() if original.get(k, "") != v}
        if not changes:
            p.write("\nNothing changed.")
            return 0
        _summary(p, changes)
        if not p.confirm(f"Save to {env_file}?", default=True):
            p.write("Not saved.")
            return 1
    except WizardAborted:
        p.write("\nAborted. Nothing was saved.")
        return 130

    backup = envfile.save(env_file, changes)
    p.write(f"\nSaved {env_file}" + (f" (previous version: {backup})" if backup else ""))
    if after_save:
        p.write("\nChecking the configuration...\n")
        subprocess.run(_doctor_command(), check=False)
        try:
            _offer_service_restart(p)
        except WizardAborted:
            pass
    return 0

"""Agent discovery: every folder agents/<name>/agent.py or sre/<name>/agent.py
is an agent exposing `run(**kwargs)`."""
import importlib
import os
from pathlib import Path
from types import ModuleType

_ROOT = Path(__file__).parent
_SRE = _ROOT.parent / "sre"


def _agent_dirs() -> list[tuple[str, Path]]:
    """(import package, directory) pairs searched for agents."""
    return [("sre_agent.agents", _ROOT), ("sre_agent.sre", _SRE)]


def is_enabled(name: str) -> bool:
    """<NAME>_ENABLED=false in .env switches an agent off (default on): MONITOR_ENABLED, CODING_ENABLED, ..."""
    return os.getenv(f"{name.upper()}_ENABLED", "true").strip().lower() not in ("false", "0", "no", "off")


def agent_names(include_template: bool = False) -> list[str]:
    """Installed AND enabled agents. A disabled agent behaves exactly like a removed folder."""
    names = set()
    for _, root in _agent_dirs():
        if not root.is_dir():
            continue
        names.update(
            p.parent.name for p in root.glob("*/agent.py")
            if include_template or not p.parent.name.startswith("_")
        )
    return sorted(n for n in names if n.startswith("_") or is_enabled(n))


def load_agent(name: str) -> ModuleType:
    if not name.startswith("_") and not is_enabled(name):
        raise ModuleNotFoundError(f"agent '{name}' is disabled ({name.upper()}_ENABLED=false)")
    for pkg, root in _agent_dirs():
        if (root / name / "agent.py").is_file():
            return importlib.import_module(f"{pkg}.{name}.agent")
    raise ModuleNotFoundError(f"unknown agent '{name}'")


def load_all() -> dict[str, ModuleType]:
    """Import every agent (this also registers their @on_approval handlers)."""
    return {n: load_agent(n) for n in agent_names(include_template=True)}

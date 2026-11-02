"""Where config and state live. Imported before anything reads os.environ.

    SRE_ENV_FILE  config file to load (default ./.env). Packages set /etc/sre-agent/sre-agent.env.
    SRE_HOME      state directory: DB, signals, repo clone, worktrees, logs (default ./data).

Variables already in the environment (systemd EnvironmentFile, docker -e, shell) win over the file.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

_loaded = False


def load_env() -> None:
    """Load SRE_ENV_FILE once (idempotent)."""
    global _loaded
    if _loaded:
        return
    load_dotenv(os.getenv("SRE_ENV_FILE", ".env"), override=False)
    _loaded = True


def sre_home() -> Path:
    load_env()
    return Path(os.getenv("SRE_HOME", "./data").strip() or "./data").expanduser().resolve()


def home_path(env_key: str, default_rel: str) -> Path:
    """Path from env_key, else SRE_HOME/default_rel."""
    load_env()
    raw = os.getenv(env_key, "").strip()
    return Path(raw).expanduser().resolve() if raw else sre_home() / default_rel

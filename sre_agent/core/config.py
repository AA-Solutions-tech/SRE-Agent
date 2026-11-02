import os
from dataclasses import dataclass
from pathlib import Path
from .env import home_path, load_env, sre_home

load_env()


def _e(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _list(key: str) -> list[str]:
    return [x.strip() for x in _e(key).split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    llm_provider: str = _e("LLM_PROVIDER", "anthropic").lower()  # anthropic | openai | xai | gemini | openai_compatible
    llm_model: str = _e("LLM_MODEL")  # empty = provider default (see core/llm.py)
    openai_base_url: str = _e("OPENAI_BASE_URL")  # only for openai_compatible (Ollama, Groq, Together, vLLM...)

    slack_webhook: str = _e("SLACK_WEBHOOK_URL")
    gchat_webhook: str = _e("GCHAT_WEBHOOK_URL")
    smtp_host: str = _e("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(_e("SMTP_PORT", "587"))
    smtp_user: str = _e("SMTP_USER")
    smtp_password: str = _e("SMTP_PASSWORD")
    email_from: str = _e("EMAIL_FROM")
    email_to: tuple = tuple(_list("EMAIL_TO"))
    notify_channels: tuple = tuple(_list("NOTIFY_CHANNELS") or ["slack"])

    app_base_url: str = _e("APP_BASE_URL", "http://localhost:8000").rstrip("/")
    approval_secret: str = _e("APPROVAL_SECRET", "change-me")

    ssh_host: str = _e("SSH_HOST")
    ssh_port: int = int(_e("SSH_PORT", "22"))
    ssh_user: str = _e("SSH_USER")
    ssh_key_path: str = os.path.expanduser(_e("SSH_KEY_PATH", "~/.ssh/id_rsa"))
    remote_log_path: str = _e("REMOTE_LOG_PATH", "/var/log/app/error.log")
    ssh_known_hosts: str = os.path.expanduser(_e("SSH_KNOWN_HOSTS", "~/.ssh/known_hosts"))
    ssh_strict_host_key: bool = _e("SSH_STRICT_HOST_KEY", "true").lower() not in ("false", "0", "no", "off")
    poll_seconds: int = int(_e("POLL_SECONDS", "5"))

    repo_dir: Path = home_path("REPO_DIR", "repo")
    worktrees_dir: Path = home_path("WORKTREES_DIR", "worktrees")
    base_branch: str = _e("BASE_BRANCH", "main")
    branch_prefix: str = _e("BRANCH_PREFIX", "ai-fix")
    test_cmd: str = _e("TEST_CMD")
    dry_run_push: bool = _e("DRY_RUN_PUSH", "true").lower() == "true"

    github_token: str = _e("GITHUB_TOKEN")
    github_repos: tuple = tuple(_list("GITHUB_REPOS"))
    jira_base_url: str = _e("JIRA_BASE_URL").rstrip("/")
    jira_email: str = _e("JIRA_EMAIL")
    jira_api_token: str = _e("JIRA_API_TOKEN")
    jira_projects: tuple = tuple(_list("JIRA_PROJECTS"))

    # ---- Coding agent / Agent 3 (append-only block; other agents add their own blocks) ----
    coding_max_fix_iters: int = int(_e("CODING_MAX_FIX_ITERS", "2"))
    coding_llm_retries: int = int(_e("CODING_LLM_RETRIES", "2"))
    coding_test_timeout: int = int(_e("CODING_TEST_TIMEOUT", "180"))
    coding_min_confidence: float = float(_e("CODING_MIN_CONFIDENCE", "0.5"))
    coding_allowed_paths: tuple = tuple(_list("CODING_ALLOWED_PATHS"))  # empty = all (minus BLOCKED_PARTS)
    verify_window_seconds: int = int(_e("VERIFY_WINDOW_SECONDS", "300"))
    max_auto_rounds: int = int(_e("MAX_AUTO_ROUNDS", "2"))

    data_dir: Path = sre_home()
    db_path: str = str(home_path("SRE_DB_PATH", "starter.db"))

    # ---- Install / server (packaging) ----
    api_key: str = _e("SRE_API_KEY")  # empty = HTTP API open (dev only; packages generate one)
    bind: str = _e("SRE_BIND", "127.0.0.1")
    port: int = int(_e("SRE_PORT", "8000"))


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)

"""The wizard's sections. Each step reads current values and returns only the keys it wants to set."""
from pathlib import Path
from types import ModuleType

from . import ssh as ssh_ops
from . import validators as v
from .prompts import Prompter

LLM_PROVIDERS = [
    ("anthropic", "Claude (ANTHROPIC_API_KEY)"),
    ("openai", "OpenAI (OPENAI_API_KEY)"),
    ("groq", "Groq (GROQ_API_KEY)"),
    ("xai", "xAI Grok (XAI_API_KEY)"),
    ("openai_compatible", "Ollama, vLLM, Together... (OpenAI-compatible URL)"),
]
LLM_KEY = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "groq": "GROQ_API_KEY",
           "xai": "XAI_API_KEY"}
CHANNELS = [("slack", "Slack incoming webhook"), ("gchat", "Google Chat webhook"), ("email", "Email over SMTP")]
DEFAULT_KEY = "/var/lib/sre-agent/.ssh/id_ed25519"
DEFAULT_KNOWN_HOSTS = "/var/lib/sre-agent/.ssh/known_hosts"


def llm(p: Prompter, cur: dict) -> dict:
    p.heading("AI provider")
    provider = p.choice("Provider", LLM_PROVIDERS, current=cur.get("LLM_PROVIDER", ""), default="anthropic")
    out = {"LLM_PROVIDER": provider}
    if provider == "openai_compatible":
        out["OPENAI_BASE_URL"] = p.text("Base URL", current=cur.get("OPENAI_BASE_URL", ""), required=True,
                                        help="e.g. http://localhost:11434/v1", validate=v.url)
        out["OPENAI_API_KEY"] = p.text("API key", current=cur.get("OPENAI_API_KEY", ""), secret=True)
    else:
        key = LLM_KEY[provider]
        out[key] = p.text(key, current=cur.get(key, ""), required=True, secret=True,
                          help="Typing is hidden. Paste the key and press Enter.", validate=v.no_spaces)
    out["LLM_MODEL"] = p.text("Model", current=cur.get("LLM_MODEL", ""), help="Empty = provider default.")
    return out


def notifications(p: Prompter, cur: dict) -> dict:
    p.heading("Where incidents and approvals are sent")
    current = [c.strip() for c in cur.get("NOTIFY_CHANNELS", "").split(",") if c.strip()]
    picked = p.multi("Channels (comma separated)", CHANNELS, current=current)
    out = {"NOTIFY_CHANNELS": ",".join(picked)}
    if "slack" in picked:
        out["SLACK_WEBHOOK_URL"] = p.text("Slack webhook URL", current=cur.get("SLACK_WEBHOOK_URL", ""),
                                          required=True, secret=True, validate=v.https_url)
    if "gchat" in picked:
        out["GCHAT_WEBHOOK_URL"] = p.text("Google Chat webhook URL", current=cur.get("GCHAT_WEBHOOK_URL", ""),
                                          required=True, secret=True, validate=v.https_url)
    if "email" in picked:
        out["SMTP_HOST"] = p.text("SMTP host", current=cur.get("SMTP_HOST", ""), default="smtp.gmail.com",
                                  required=True, validate=v.host)
        out["SMTP_PORT"] = p.text("SMTP port", current=cur.get("SMTP_PORT", ""), default="587", validate=v.port)
        out["SMTP_USER"] = p.text("SMTP user", current=cur.get("SMTP_USER", ""), required=True)
        out["SMTP_PASSWORD"] = p.text("SMTP password", current=cur.get("SMTP_PASSWORD", ""), required=True,
                                      secret=True)
        out["EMAIL_FROM"] = p.text("From address", current=cur.get("EMAIL_FROM", ""),
                                   default=out["SMTP_USER"] if "@" in out["SMTP_USER"] else "", validate=v.email)
        out["EMAIL_TO"] = p.text("Send to (comma separated)", current=cur.get("EMAIL_TO", ""), required=True,
                                 validate=v.email_list)
    return out


def _ssh_trust(p: Prompter, out: dict, ops: ModuleType) -> None:
    key_path = Path(out["SSH_KEY_PATH"])
    if not key_path.exists() and p.confirm("No SSH key there yet. Create one for the agent now?", default=True):
        try:
            pub = ops.generate_key(key_path)
            p.info("Add this public key to ~/.ssh/authorized_keys of "
                   f"{out['SSH_USER']} on {out['SSH_HOST']} (a read-only user is enough):")
            p.write("")
            p.write(pub)
            p.write("")
        except Exception as e:  # noqa: BLE001  missing ssh-keygen, permissions...
            p.info(f"Could not create the key: {e}")
    host, port = out["SSH_HOST"], int(out["SSH_PORT"] or 22)
    if not p.confirm(f"Connect to {host}:{port} now to trust its host key?", default=True):
        return
    try:
        key_type, key_b64, fingerprint = ops.fetch_host_key(host, port)
    except Exception as e:  # noqa: BLE001  any network/SSH failure only skips this optional step
        p.info(f"Could not reach {host}:{port} ({e}). Add it later with ssh-keyscan.")
        return
    p.info(f"The server presented: {key_type} {fingerprint}")
    p.info("Compare it with the server (ssh-keygen -lf /etc/ssh/ssh_host_*_key.pub) before accepting.")
    if p.confirm("Does the fingerprint match?", default=False):
        added = ops.add_known_host(Path(out["SSH_KNOWN_HOSTS"]), ops.known_hosts_line(host, port, key_type, key_b64))
        p.info("Host key saved." if added else "Host key was already trusted.")


def logs(p: Prompter, cur: dict, ops: ModuleType = ssh_ops) -> dict:
    p.heading("Which application logs to watch")
    source = p.choice("Log source", [("ssh", "Read a log file on another server over SSH"),
                                     ("local_file", "Read a log file on this server")],
                      current=cur.get("MONITOR_SOURCE", ""), default="ssh")
    out = {"MONITOR_SOURCE": source}
    out["MONITOR_SERVICE"] = p.text("Service name (shown in incidents)", current=cur.get("MONITOR_SERVICE", ""),
                                    required=True, validate=v.no_spaces)
    if source == "local_file":
        out["MONITOR_LOG_PATH"] = p.text("Log file path", current=cur.get("MONITOR_LOG_PATH", ""), required=True,
                                         help="Also accepts {date} or a wildcard, e.g. /var/log/app/errors-*.jsonl",
                                         validate=v.absolute_path)
        return out
    out["SSH_HOST"] = p.text("Server host or IP", current=cur.get("SSH_HOST", ""), required=True, validate=v.host)
    out["SSH_PORT"] = p.text("SSH port", current=cur.get("SSH_PORT", ""), default="22", validate=v.port)
    out["SSH_USER"] = p.text("SSH user", current=cur.get("SSH_USER", ""), required=True, validate=v.no_spaces)
    out["REMOTE_LOG_PATH"] = p.text("Log file path on that server", current=cur.get("REMOTE_LOG_PATH", ""),
                                    required=True, validate=v.absolute_path,
                                    help="Also accepts {date} or a wildcard, e.g. /var/log/app/errors-*.jsonl")
    out["SSH_KEY_PATH"] = p.text("Private key file", current=cur.get("SSH_KEY_PATH", ""), default=DEFAULT_KEY,
                                 validate=v.absolute_path)
    out["SSH_KNOWN_HOSTS"] = p.text("known_hosts file", current=cur.get("SSH_KNOWN_HOSTS", ""),
                                    default=DEFAULT_KNOWN_HOSTS, validate=v.absolute_path)
    _ssh_trust(p, out, ops)
    return out


def web(p: Prompter, cur: dict) -> dict:
    p.heading("Web access (approval links)")
    p.info("People click Approve/Reject links in Slack or email; they open APP_BASE_URL.")
    p.info("Use the public HTTPS address of your reverse proxy (nginx/Caddy) in production.")
    out = {"APP_BASE_URL": p.text("Public URL", current=cur.get("APP_BASE_URL", ""),
                                  default="http://localhost:8000", validate=v.url)}
    out["SRE_BIND"] = p.choice("Listen on", [("127.0.0.1", "this machine only, behind a reverse proxy (safer)"),
                                             ("0.0.0.0", "all network interfaces")],
                               current=cur.get("SRE_BIND", ""), default="127.0.0.1")
    out["SRE_PORT"] = p.text("Port", current=cur.get("SRE_PORT", ""), default="8000", validate=v.port)
    return out


def code(p: Prompter, cur: dict) -> dict:
    p.heading("Code fixes (optional)")
    configured = bool(cur.get("GITHUB_TOKEN") or cur.get("TEST_CMD") or cur.get("REPO_DIR"))
    if not p.confirm("Configure the repository, tests and GitHub pull requests now?", default=configured):
        return {}
    return {
        "REPO_DIR": p.text("Repo clone path", current=cur.get("REPO_DIR", ""), validate=v.absolute_path,
                           help="Empty = /var/lib/sre-agent/repo (clone it there as the sre-agent user)."),
        "BASE_BRANCH": p.text("Base branch", current=cur.get("BASE_BRANCH", ""), default="main",
                              validate=v.no_spaces),
        "TEST_CMD": p.text("Test command", current=cur.get("TEST_CMD", ""),
                           help="Run in the fix's worktree, e.g. npm test -- --ci. Must be on the service PATH."),
        "LINT_CMD": p.text("Lint command", current=cur.get("LINT_CMD", "")),
        "GITHUB_TOKEN": p.text("GitHub token (for pull requests)", current=cur.get("GITHUB_TOKEN", ""),
                               secret=True, validate=v.no_spaces),
    }


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _is_true(value: str, default: bool) -> bool:
    return default if value == "" else value.strip().lower() in ("true", "1", "yes", "on")


def automation(p: Prompter, cur: dict) -> dict:
    p.heading("Automation (optional)")
    p.info("First installs keep everything manual. Turn things on once you trust the results.")
    if not p.confirm("Change the automation switches now?", default=False):
        return {}
    out = {
        "SRE_AUTO_RUN": _flag(p.confirm("Watch the logs continuously (auto mode)?",
                                        default=_is_true(cur.get("SRE_AUTO_RUN", ""), False))),
        "CODING_ENABLED": _flag(p.confirm("Let the agent write code fixes?",
                                          default=_is_true(cur.get("CODING_ENABLED", ""), False))),
    }
    if out["CODING_ENABLED"] == "true":
        push = p.confirm("Push approved fixes to the remote?", default=not _is_true(cur.get("DRY_RUN_PUSH", ""), True))
        out["DRY_RUN_PUSH"] = _flag(not push)
        out["SHIP_MODE"] = p.choice("Shipping", [("approve", "ask a person before every commit/push/PR"),
                                                  ("auto", "ship low-risk fixes without a second approval")],
                                    current=cur.get("SHIP_MODE", ""), default="approve")
    return out


STEPS = {
    "llm": llm,
    "notifications": notifications,
    "logs": logs,
    "web": web,
    "code": code,
    "automation": automation,
}

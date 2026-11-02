# SRE Agent

An AI site-reliability agent: it **watches your application logs, raises incidents, finds the root cause, proposes a
code fix and opens a pull request**, with a human approving every step (Slack / Google Chat / email buttons).
Built on LangChain + LangGraph with swappable LLMs (Claude, OpenAI, Groq, xAI, any OpenAI-compatible server).

For a step-by-step production walk-through see **[docs/SRE-Agent-Deployment-Guide.md](docs/SRE-Agent-Deployment-Guide.md)** (also as a styled page: [HTML](docs/SRE-Agent-Deployment-Guide.html), open it in a browser after cloning).

## Contents
1. [Install on a Linux server](#install-on-a-linux-server)
2. [Configure and start](#configure-and-start)
3. [Verify](#verify)
4. [Operate, upgrade, uninstall](#operate-upgrade-uninstall)
5. [Build the packages yourself](#build-the-packages-yourself)
6. [Publish a release](#publish-a-release)
7. [Development](#setup-for-development-2-min)

## Install on a Linux server

| System | Package |
|---|---|
| Ubuntu 22.04 / 24.04, Debian 12 | `sre-agent_<version>-1_amd64.deb` / `_arm64.deb` |
| RHEL / Rocky / Alma 8 and 9, Amazon Linux 2023 | `sre-agent-<version>-1.x86_64.rpm` / `.aarch64.rpm` |

The package brings its own Python. The server only needs `git` (installed automatically as a dependency).
Packages are on the **[Releases](https://github.com/AA-Solutions-tech/SRE-Agent/releases)** page.

```bash
VERSION=2.0.0

# Ubuntu / Debian
wget https://github.com/AA-Solutions-tech/SRE-Agent/releases/download/v$VERSION/sre-agent_$VERSION-1_amd64.deb
sudo apt install ./sre-agent_$VERSION-1_amd64.deb

# RHEL / Rocky / Alma / Amazon Linux
sudo dnf install https://github.com/AA-Solutions-tech/SRE-Agent/releases/download/v$VERSION/sre-agent-$VERSION-1.x86_64.rpm
```

Optional checksum: download `SHA256SUMS` from the same release, then `sha256sum -c --ignore-missing SHA256SUMS`.

The install creates the `sre-agent` user, `/opt/sre-agent`, `/etc/sre-agent/sre-agent.env` (with a unique
`SRE_API_KEY` and `APPROVAL_SECRET` for this server) and an enabled but **not started** systemd service.

## Configure and start

Have ready: an AI provider API key, a Slack/Google Chat webhook or SMTP details, and the path of your app's error log.

```bash
sudo sre-agent setup
```

The setup asks a few questions, one section at a time, and writes `/etc/sre-agent/sre-agent.env` for you:

| Section | Asks for | Required |
|---|---|---|
| AI provider | Claude / OpenAI / Groq / xAI / OpenAI-compatible, and its API key | yes |
| Alerts | Slack and/or Google Chat webhook, or email (SMTP) | at least one |
| Logs | a log file on this server, or on another server over SSH (it can create the SSH key and trust the server for you) | yes |
| Web access | public URL for the Approve/Reject links, listen address, port | defaults offered |
| Code fixes | repo path, branch, test command, GitHub token | optional |
| Automation | auto mode, code fixes, pushing, shipping mode | optional (all off at first) |

Press **Enter** to keep the value in brackets or skip an optional question, type **-** to clear a value,
**Ctrl+C** to quit without saving. Secrets are typed hidden and shown masked. At the end it shows a summary, saves
(keeping a `.bak` copy), runs `sre-agent doctor` and offers to start the service.

**Turn on "Watch the logs continuously (auto mode)"**, otherwise the service runs but does not monitor the log.
Change one part later with `sudo sre-agent setup --only notifications`
(sections: `llm`, `notifications`, `logs`, `web`, `code`, `automation`). Prefer editing by hand?
`sudo nano /etc/sre-agent/sre-agent.env`, then `sudo sre-agent doctor` and `sudo systemctl restart sre-agent`.

If the log is on this server, the `sre-agent` user must be able to read it
(`sudo setfacl -m u:sre-agent:rx <folder>` and `sudo setfacl -m u:sre-agent:r <file>`); details are in the deployment guide.

```bash
sudo systemctl start sre-agent        # skip if setup already started it
sudo systemctl status sre-agent       # active (running); it also starts at boot
```

Prefer pm2? See Step 5B of the deployment guide (use systemd **or** pm2, not both).

## Verify

```bash
sudo sre-agent doctor                                     # all required checks OK
curl -s localhost:8000/health                             # {"ok":true,"version":"2.0.0",...}
KEY=$(sudo sed -n 's/^SRE_API_KEY=\([^ ]*\).*/\1/p' /etc/sre-agent/sre-agent.env)
curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/issues                        # 401
curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: $KEY" localhost:8000/issues   # 200
```

End-to-end test: append a test error to the watched log. An incident card should arrive in your channel and show in
`/issues`:

```bash
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [ERROR] [my-service] SreAgentTest: test error, please ignore" | sudo tee -a /var/log/my-service/error.log
```

Supported error-line formats: bracketed `[time] [ERROR] [service] message`, JSON lines with a text `level`, and
`YYYY-MM-DD HH:MM:SS:ms [error] message`.

## Operate, upgrade, uninstall

| What | Where / command |
|---|---|
| Config | `/etc/sre-agent/sre-agent.env` |
| Data (DB, repo clone, backups) | `/var/lib/sre-agent` |
| Logs | `journalctl -u sre-agent -f` and `/var/log/sre-agent/sre.log` |
| SSH key for reading remote logs | `/var/lib/sre-agent/.ssh/` |
| API | header `X-API-Key: <SRE_API_KEY>` |

**Upgrade:** install the newer package the same way. Config and data are kept, the database is backed up to
`/var/lib/sre-agent/backups/` first, and a running service is restarted.

**Uninstall:**
```bash
sudo apt remove sre-agent      # keeps config and data      (dnf remove sre-agent on RHEL)
sudo apt purge sre-agent       # also deletes config, logs and the service user; data in /var/lib/sre-agent stays
```

## Build the packages yourself

Needs Docker (any host: Linux, macOS, Windows Git Bash).

```bash
packaging/build-linux.sh amd64                              # dist/*.deb + dist/*.rpm (~1 min)
packaging/build-linux.sh arm64                              # arm64 (Docker with QEMU, default in Docker Desktop)
EXTRA_VERSION=2.0.0.99 packaging/build-linux.sh amd64       # + a "newer" copy for upgrade tests
packaging/linux/test/run-smoke.sh                           # install/upgrade/remove on 6 distros
packaging/build-linux.sh lock                               # regenerate the hash-pinned dependency lock
```

The version comes from `sre_agent/__init__.py`.

## Publish a release

1. Bump `__version__` in `sre_agent/__init__.py` and merge to `main`.
2. Tag it: `git tag v2.0.1 && git push origin v2.0.1`.
3. The `linux-packages` workflow builds amd64 and arm64, smoke-tests on 6 distros plus a real systemd run, and
   publishes the `.deb`, `.rpm` and `SHA256SUMS` as a GitHub Release.

## Setup for development (2 min)
```bash
python -m venv venv && source venv/Scripts/activate      # Windows Git Bash (Linux/Mac: venv/bin/activate)
pip install -r requirements.txt                          # or: pip install -e ".[dev]"
cp .env.example .env        # fill in: one LLM key + SLACK_WEBHOOK_URL (everything else is optional)
python -m sre_agent run --list                           # same as: python run.py --list
python -m sre_agent run hello "what is 2+2?"
python -m sre_agent setup                                # interactive config (writes .env)
python -m sre_agent doctor                               # checks config, paths, git, keys
pytest
```
Connectors that aren't configured just print to the console, so you can start without any webhooks.

## Build your agent
```bash
cp -r sre_agent/agents/_template sre_agent/agents/my_agent     # edit agent.py (search TODO(you))
python run.py my_agent "input"
```
An agent = folder `sre_agent/agents/<name>/agent.py` (or `sre_agent/sre/<name>/agent.py`) with a `run(...)` function.
It's auto-discovered by the CLI and the server.

## Layout
| Path | What |
|---|---|
| `sre_agent/core/llm.py` | `get_llm()` - provider chosen by `LLM_PROVIDER` |
| `sre_agent/core/tools.py` | LangChain tools: `github_tools()`, `jira_tools()`, `chat_tools()` (`write=True` for write access) |
| `sre_agent/core/notify.py` | `notify(title, body, buttons)` to Slack / GChat / email + signed approval links |
| `sre_agent/core/approvals.py` | `request_approval()` + `@on_approval(stage)` human-in-the-loop |
| `sre_agent/core/db.py` | SQLite item store (`create_item`, `get_item`, `update_item`, `list_items`) |
| `sre_agent/core/git_tools.py` | Sandboxed read/write repo tools + worktree/commit/push helpers (push = human-approved code only) |
| `sre_agent/core/ssh_logs.py` | `RemoteLogReader().read_new()` - tail a remote log over SSH |
| `sre_agent/agents/hello` | Minimal ReAct agent with tools |
| `sre_agent/agents/manager_report` | GitHub+Jira+standups -> LLM report -> chat (LangGraph pipeline) |
| `sre_agent/agents/_template` | Copy me: analyze -> propose -> human approval -> act |
| `sre_agent/server.py` | FastAPI: `/approve/...` links, `/run/{agent}`, `/health` (root `server.py` is a dev shim) |
| `sre_agent/cli.py` | `sre-agent setup / serve / run / doctor / paths / gen-secret / version` |
| `sre_agent/wizard/` | `sre-agent setup`: interactive questions that write the config file |
| `sre_agent/doctor.py` | Install checks (`--quick` runs before the service starts) |
| `sre_agent/core/env.py` | `SRE_ENV_FILE` (config file) and `SRE_HOME` (data dir, default `./data`) |
| `sre_agent/core/auth.py` | `SRE_API_KEY` check (`X-API-Key` header) for `/run`, `/agents`, `/signals`, `/issues` |
| `packaging/` | `.deb`/`.rpm` build (nFPM), systemd unit, install scripts, smoke tests, hash-pinned lock |

## Approvals
```bash
uvicorn server:app --port 8000      # use `ngrok http 8000` + set APP_BASE_URL so teammates can click links
```
Links are HMAC-signed and single-use. Set `APPROVAL_SECRET` in `.env` (`sre-agent serve` refuses the default).
Set `SRE_API_KEY` to protect the API; clients then send `X-API-Key: <key>`. Empty = open (local dev only).

## Extending (copy-paste recipes)
Prefer **new files** over editing shared ones (avoids merge conflicts when everyone works at once).

**New tool for your agent** (`sre_agent/agents/my_agent/tools.py`):
```python
from langchain_core.tools import tool

@tool
def lookup_customer(email: str) -> str:
    """Find a customer by email. The LLM reads this docstring to decide when to call it."""
    return "..."
```
Then: `create_react_agent(get_llm(), github_tools() + [lookup_customer], prompt=SYSTEM)`.

**New LLM provider** - add a branch in `sre_agent/core/llm.py::get_llm` and a default in `DEFAULT_MODELS`.

**New notification channel (Teams, Discord...)** - add `send_teams()` in `sre_agent/core/notify.py` and an `elif ch == "teams"` in `notify()`.

**New approval step** - `@on_approval("deploy")` handler + `request_approval(item_id, "deploy", title, body)`.

**Trigger an agent** - CLI `python run.py my_agent ...`, HTTP `POST /run/my_agent {"arg": "..."}`, or cron calling the CLI.

**Agent shapes** - ReAct loop (`sre_agent/agents/hello`), fixed pipeline (`sre_agent/agents/manager_report`), propose->approve->act (`sre_agent/agents/_template`).
Multi-agent / routers are just more nodes in your `StateGraph`.

## Tests
`pytest` runs offline (HTTP, SSH and LLM are mocked; git tests use a temp repo). Add tests for your agent in `tests/test_<name>.py`;
`tests/conftest.py` gives you `cfg(field=value)` to override settings and an isolated SQLite DB per test.

## Ground rules
- Agents *propose*; side effects live in `@on_approval` handlers. Keep `DRY_RUN_PUSH=true` until trusted.
- Never commit `.env`. Give agents read-only tools by default.
- Other LLM-based ideas: PR reviewer (`gh_get_pr_diff`), standup summarizer, Jira triager, log-error -> fix PR (`ssh_logs` + `git_tools`).

# SRE Agent: install packages

Installable packages for **SRE Agent** (log monitor -> incident analysis -> AI fix -> pull request).
This repository only holds release downloads; see **[Releases](https://github.com/AA-Solutions-tech/SRE-Agent/releases)**.

**Guide:** the step-by-step [deployment guide](docs/SRE-Agent-Deployment-Guide.md) covers a new install and an upgrade
([HTML version](docs/SRE-Agent-Deployment-Guide.html), open it in a browser after downloading).

## Supported systems

| System | Package |
|---|---|
| Ubuntu 22.04 / 24.04, Debian 12 | `sre-agent_<version>-1_amd64.deb` / `_arm64.deb` |
| RHEL / Rocky / Alma 8 and 9, Amazon Linux 2023 | `sre-agent-<version>-1.x86_64.rpm` / `.aarch64.rpm` |

The package brings its own Python. The server only needs `git` (installed automatically as a dependency).

## Install

```bash
VERSION=1.1.0

# Ubuntu / Debian
wget https://github.com/AA-Solutions-tech/SRE-Agent/releases/download/v$VERSION/sre-agent_$VERSION-1_amd64.deb
sudo apt install ./sre-agent_$VERSION-1_amd64.deb

# RHEL / Rocky / Alma / Amazon Linux
sudo dnf install https://github.com/AA-Solutions-tech/SRE-Agent/releases/download/v$VERSION/sre-agent-$VERSION-1.x86_64.rpm
```

Verify the download (optional): download `SHA256SUMS` from the same release and run `sha256sum -c --ignore-missing SHA256SUMS`.

## Configure and start

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

Press **Enter** to keep the value shown in brackets or skip an optional question, type **-** to clear a value,
**Ctrl+C** to quit without saving. Secrets are typed hidden and shown masked. At the end it shows a summary,
saves (keeping a `.bak` copy), runs `sre-agent doctor` and offers to start the service.

Change one part later with e.g. `sudo sre-agent setup --only notifications`
(sections: `llm`, `notifications`, `logs`, `web`, `code`, `automation`).

Prefer editing by hand? `sudo nano /etc/sre-agent/sre-agent.env`, then `sudo sre-agent doctor` and
`sudo systemctl restart sre-agent`.

`SRE_API_KEY` and `APPROVAL_SECRET` are generated for you on first install (unique per server).
Call the API with the header `X-API-Key: <SRE_API_KEY>`.

| What | Where |
|---|---|
| Config | `/etc/sre-agent/sre-agent.env` |
| Data (DB, repo clone, backups) | `/var/lib/sre-agent` |
| Logs | `journalctl -u sre-agent -f` and `/var/log/sre-agent/sre.log` |
| SSH key for reading remote logs | `/var/lib/sre-agent/.ssh/` |

## Upgrade

Install the newer package the same way. Config and data are kept, the database is backed up to
`/var/lib/sre-agent/backups/` first, and a running service is restarted automatically (pm2 users run
`sudo pm2 restart sre-agent`). Then run `sudo sre-agent doctor`. There is no apt/dnf repository, so download each new
version from [Releases](https://github.com/AA-Solutions-tech/SRE-Agent/releases).

## Uninstall

```bash
sudo apt remove sre-agent      # keeps config and data      (dnf remove sre-agent on RHEL)
sudo apt purge sre-agent       # also deletes config, logs and the service user; data in /var/lib/sre-agent stays
```

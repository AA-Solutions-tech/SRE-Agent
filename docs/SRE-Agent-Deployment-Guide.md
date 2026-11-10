# SRE Agent: deployment guide (Ubuntu / Debian / RHEL, systemd or pm2)

Version 2.2.0 · For anyone who installs the `.deb` or `.rpm` package on one Linux server and runs background services
with **systemd** or **pm2**.

Time needed: about 15 minutes (2 to install, 5 for the questions, the rest for checks).

---

## Step 0. Have these ready before you start

| # | What | Why | Example |
|---|---|---|---|
| 1 | A Linux server: Ubuntu 22.04 / 24.04 or Debian 12, x86_64, with `sudo` | The agent runs here | `ubuntu@ops-server` |
| 2 | An AI provider API key | Root-cause analysis and fixes | Anthropic `sk-ant-...`, OpenAI `sk-...`, Groq `gsk_...` |
| 3 | Where alerts should go: a Slack incoming webhook, a Google Chat webhook, or SMTP email details | Incident cards with Approve / Reject buttons | `https://hooks.slack.com/services/T000/B000/XXXX` |
| 4 | The **error log file** of your application, and its **service name** | What the agent watches | `/var/log/orders-api/error.log`, `orders-api` |
| 5 | If the log is on **another** server: its host/IP, an SSH user that can read the log | Read-only access over SSH | `10.0.1.25`, `logreader` |
| 6 | Optional: the public HTTPS address people will click in Slack/email | Approve / Reject links | `https://sre.yourcompany.com` |
| 7 | Optional: Git repo of the app, its test command, a GitHub token | Automatic fixes as pull requests | `npm test -- --ci`, `ghp_...` |

### Check your log format first (important)

The agent raises incidents only for log lines it can read. Each error line must be in **one** of these formats:

| Format | Example line |
|---|---|
| Bracketed | `[2026-10-27T10:00:00Z] [ERROR] [orders-api] DatabaseTimeout: connection refused` |
| JSON lines (one JSON object per line, e.g. winston) | `{"level":"error","message":"PaymentFailed: gateway 502","service":"orders-api","timestamp":"2026-10-27T10:00:05Z"}` |
| Node logger style | `2026-10-27 10:00:01:184 [error] Error fetching report metrics: fetch failed` |

- `ERROR`, `FATAL`, `CRITICAL` become **high** severity, `WARN` becomes **medium**. `INFO` and `DEBUG` are
  ignored.
- Other lines, such as raw stack traces (`TypeError: ... at handler (...)`), never raise an incident on their own.
  They are attached as context when a proper error line fires.
- JSON loggers must write `level` as **text** (`"error"`), not a number. Pino's default numeric levels
  (`"level":50`) are not recognised; configure pino to print level labels.
- **pm2 users:** the `pm2 logs` error file contains whatever your app prints. If your app uses `console.error`
  with plain text, switch it to a structured logger (winston JSON is the easiest) or the bracketed format above.

---

## Step 1. Install the package

```bash
wget https://github.com/AA-Solutions-tech/SRE-Agent/releases/download/v2.2.0/sre-agent_2.2.0-1_amd64.deb
wget https://github.com/AA-Solutions-tech/SRE-Agent/releases/download/v2.2.0/SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS          # expect: sre-agent_2.2.0-1_amd64.deb: OK
sudo apt install ./sre-agent_2.2.0-1_amd64.deb
```

You should see this at the end:

```
sre-agent: created /etc/sre-agent/sre-agent.env

SRE Agent 2.2.0 installed. The service is enabled but NOT started yet.
  Configure it:   sudo sre-agent setup
```

**Verify the install**

```bash
sre-agent version                     # sre-agent 2.2.0
id sre-agent                          # the service user exists
sudo ls -l /etc/sre-agent/            # sre-agent.env  -rw-r----- root sre-agent
```

**RHEL / Rocky / Alma 8-9 / Amazon Linux 2023** use the `.rpm` instead (install `acl` with `dnf` in Step 3):

```bash
sudo dnf install https://github.com/AA-Solutions-tech/SRE-Agent/releases/download/v2.2.0/sre-agent-2.2.0-1.x86_64.rpm
```

On arm64 servers use `sre-agent_2.2.0-1_arm64.deb` / `sre-agent-2.2.0-1.aarch64.rpm`.

What the install did: it put the program in `/opt/sre-agent`, created the `sre-agent` system user, created the
config file with a **unique API key and approval secret for this server**, and registered the `sre-agent` systemd
service (enabled, not started).

---

## Step 2. Choose how it will run: systemd or pm2

| | systemd (recommended) | pm2 |
|---|---|---|
| Already set up by the package | Yes | No, one extra command |
| Starts at boot | Yes | Yes, with `pm2 startup` + `pm2 save` |
| Security hardening | Yes (read-only system, private tmp, no extra privileges) | No; runs as the `sre-agent` user only |
| Logs | `journalctl -u sre-agent` + `/var/log/sre-agent/sre.log` | `pm2 logs sre-agent` + `/var/log/sre-agent/sre.log` |
| Runs a config check before each start | Yes | No, but it still refuses to start with default secrets |

Use **one** of them, not both. If both try to run, the second one refuses to start ("another sre-agent is already
running").

---

## Step 3. Let the agent read your log file (same-server logs only)

Skip this step if the log is on another server (SSH).

The agent runs as the `sre-agent` user, which must be able to **read** the log file and open its folders.

**Logs under `/var/log/...`** (most common):

```bash
sudo apt install -y acl
sudo setfacl -m u:sre-agent:rx /var/log/orders-api                 # open the folder
sudo setfacl -m u:sre-agent:r /var/log/orders-api/error.log        # read the file
sudo setfacl -d -m u:sre-agent:r /var/log/orders-api               # new/rotated files too
sudo -u sre-agent head -1 /var/log/orders-api/error.log            # must print a line, not "Permission denied"
```

**pm2 logs under a home folder** (for example `/home/deploy/.pm2/logs/orders-api-error.log`):

```bash
sudo apt install -y acl
sudo setfacl -m u:sre-agent:x /home/deploy /home/deploy/.pm2       # pass through only
sudo setfacl -m u:sre-agent:rx /home/deploy/.pm2/logs
sudo setfacl -m u:sre-agent:r /home/deploy/.pm2/logs/orders-api-error.log
sudo setfacl -d -m u:sre-agent:r /home/deploy/.pm2/logs
sudo -u sre-agent head -1 /home/deploy/.pm2/logs/orders-api-error.log
```

If you will run the agent with **systemd** and the log is under `/home`, also allow the service to see `/home`
(it hides home folders by default):

```bash
sudo systemctl edit sre-agent
```
Add these lines in the editor, then save:
```ini
[Service]
ProtectHome=read-only
```

---

## Step 4. Run the guided setup

```bash
sudo sre-agent setup
```

How to answer:

- **Enter** keeps the value shown in `[brackets]`, or skips an optional question.
- **-** clears a value.
- **Ctrl+C** quits without saving anything.
- Secret answers (keys, passwords, webhooks) are typed **hidden**.
- Required questions ask again until answered.
- A wrong format (bad URL, port, email) is explained and asked again.

### 4.1 AI provider

```
  1) anthropic          Claude (ANTHROPIC_API_KEY) (current)
  2) openai             OpenAI (OPENAI_API_KEY)
  3) groq               Groq (GROQ_API_KEY)
  4) xai                xAI Grok (XAI_API_KEY)
  5) openai_compatible  Ollama, vLLM, Together... (OpenAI-compatible URL)
  Provider [anthropic]:
  ANTHROPIC_API_KEY (required):
  Model (optional, Enter to skip):
```

| Question | What to give | Required |
|---|---|---|
| Provider | The number or name, e.g. `1` | Yes (Enter keeps `anthropic`) |
| `<PROVIDER>_API_KEY` | Paste your key (hidden) | Yes |
| Base URL | Only for option 5, e.g. `http://localhost:11434/v1` | Yes for option 5 |
| Model | Leave empty for the provider's default | No |

### 4.2 Alerts

```
  1) slack              Slack incoming webhook
  2) gchat              Google Chat webhook
  3) email              Email over SMTP
  Channels (comma separated) [slack]:
  Slack webhook URL (required):
```

| Question | What to give | Required |
|---|---|---|
| Channels | `1`, `2`, `3` or a mix like `1,3` | At least one |
| Slack webhook URL | `https://hooks.slack.com/services/...` (must be https) | If Slack chosen |
| Google Chat webhook URL | `https://chat.googleapis.com/v1/spaces/...` | If Google Chat chosen |
| SMTP host / port | e.g. `smtp.gmail.com` / `587` (defaults offered) | If email chosen |
| SMTP user / password | Mailbox login; for Gmail use an **app password** | If email chosen |
| From address | Defaults to the SMTP user | If email chosen |
| Send to | Comma-separated emails, e.g. `oncall@company.com,lead@company.com` | If email chosen |

### 4.3 Logs

```
  1) ssh                Read a log file on another server over SSH (current)
  2) local_file         Read a log file on this server
  Log source [ssh]:
  Service name (shown in incidents) [my-service]:
```

**If the log is on this server** (answer `2`):

| Question | What to give | Required |
|---|---|---|
| Service name | Short name without spaces, e.g. `orders-api` | Yes |
| Log file path | Full path, e.g. `/var/log/orders-api/error.log` or `/home/deploy/.pm2/logs/orders-api-error.log`. Rotating names are allowed: `/var/log/app/errors-{date}.jsonl` or `/var/log/app/errors-*.jsonl` | Yes |

**If the log is on another server** (answer `1`):

| Question | What to give | Required |
|---|---|---|
| Service name | e.g. `orders-api` | Yes |
| Server host or IP | e.g. `10.0.1.25` (no `http://`) | Yes |
| SSH port | Enter for `22` | Default offered |
| SSH user | A user on that server that can read the log, e.g. `logreader` | Yes |
| Log file path on that server | e.g. `/var/log/orders-api/error.log` | Yes |
| Private key file / known_hosts file | Press Enter for both (defaults under `/var/lib/sre-agent/.ssh/`) | Defaults offered |
| `Create one for the agent now? [Y/n]` | Enter (yes). It prints a public key line starting `ssh-ed25519 ...`. **Copy it.** | |
| `Connect to 10.0.1.25:22 now to trust its host key? [Y/n]` | Enter (yes) | |
| `Does the fingerprint match? [y/N]` | Compare the `SHA256:...` it shows with the line of the same key type from `for f in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf $f; done` **run on the log server**. Type `y` only if they match | |

Then, **on the log server**, allow that key to log in:

```bash
sudo -u logreader mkdir -p ~logreader/.ssh
echo 'ssh-ed25519 AAAA... sre-agent@ops-server' | sudo -u logreader tee -a ~logreader/.ssh/authorized_keys
sudo chmod 700 ~logreader/.ssh && sudo chmod 600 ~logreader/.ssh/authorized_keys
```

### 4.4 Web access

```
  Public URL [http://localhost:8000]:
  1) 127.0.0.1          this machine only, behind a reverse proxy (safer) (current)
  2) 0.0.0.0            all network interfaces
  Listen on [127.0.0.1]:
  Port [8000]:
```

| Question | What to give | Required |
|---|---|---|
| Public URL | The HTTPS address of your reverse proxy, e.g. `https://sre.yourcompany.com`. For a first test, Enter keeps `http://localhost:8000` (approval links then only work on this server) | Default offered |
| Listen on | Enter (`127.0.0.1`) and put nginx/Caddy in front. Choose `2` only if there is no proxy and the port is firewalled | Default offered |
| Port | Enter for `8000`, or another free port | Default offered |

### 4.5 Code fixes (optional)

```
  Configure the repository, tests and GitHub pull requests now? [y/N]:
```

Press Enter to skip for now. If you answer `y`:

| Question | What to give |
|---|---|
| Repo clone path | Enter for `/var/lib/sre-agent/repo` (clone your repo there, see Step 6) |
| Base branch | e.g. `main` or `dev` |
| Test command | e.g. `npm test -- --ci`. The tool must be installed for the service, see Troubleshooting |
| Lint command | e.g. `npx eslint .` (optional) |
| GitHub token | Fine-grained token with Contents + Pull requests: read/write on that repo (hidden) |

### 4.6 Automation

```
  Change the automation switches now? [y/N]:
```

Answer **`y`**, then:

| Question | Recommended first answer | Why |
|---|---|---|
| `Watch the logs continuously (auto mode)?` | **`y`** | **Required for monitoring.** Without it the service runs but does not watch the log |
| `Let the agent write code fixes?` | `n` at first | Turn on once incident cards look right and Step 4.5 is configured |
| `Push approved fixes to the remote?` | `n` at first | Only asked if code fixes are on |
| `Shipping` | `approve` | A person approves every commit/push/PR |

### 4.7 Summary, save and check

```
6 setting(s) will change
------------------------
  ANTHROPIC_API_KEY  ****7890
  SLACK_WEBHOOK_URL  ****XXXX
  MONITOR_SOURCE     local_file
  MONITOR_SERVICE    orders-api
  MONITOR_LOG_PATH   /var/log/orders-api/error.log
  SRE_AUTO_RUN       true
  Save to /etc/sre-agent/sre-agent.env? [Y/n]:
```

Press Enter to save. The setup then runs the health check. All lines should be `OK` (a `WARN` for `TEST_CMD` is
fine while code fixes are off):

```
[OK  ] APPROVAL_SECRET  set
[OK  ] SRE_API_KEY      set
[OK  ] SRE_HOME         /var/lib/sre-agent
[OK  ] database         /var/lib/sre-agent/starter.db (schema v1)
[OK  ] git              /usr/bin/git
[OK  ] LLM key          ANTHROPIC_API_KEY set
[OK  ] notifications    slack
...
all required checks passed
```

Last question: `Start sre-agent now? [Y/n]`

- **systemd:** press Enter.
- **pm2:** type `n` and continue with Step 5B.

---

## Step 5. Start it

### 5A. With systemd

```bash
sudo systemctl start sre-agent          # skip if setup already started it
sudo systemctl status sre-agent         # Active: active (running)
```

It starts automatically at boot (the package enabled it).

### 5B. With pm2

pm2 must run the agent **as root** (the agent then switches itself to the `sre-agent` user). This is root's own pm2
list, separate from the list of your app user.

```bash
sudo systemctl disable --now sre-agent                                   # make sure systemd does not run it too
sudo pm2 start /usr/bin/sre-agent --name sre-agent --interpreter none -- serve
sudo pm2 save                                                            # remember it
sudo pm2 startup systemd                                                 # start root's pm2 at boot (once per server)
sudo pm2 list                                                            # sre-agent  online
```

If `sudo pm2` says "command not found" (pm2 installed with nvm), run it with your PATH:
`sudo env "PATH=$PATH" pm2 start /usr/bin/sre-agent --name sre-agent --interpreter none -- serve` (same for
`save`, `startup`, `list`).

---

## Step 6. Verify everything works

Run these in order. Each line says what you should see.

```bash
# 1. Health check: all required checks OK
sudo sre-agent doctor

# 2. Process is running as the sre-agent user
ps -o user=,cmd= -C python3 | grep sre_agent      # sre-agent  ... -m sre_agent serve

# 3. Web API answers
curl -s localhost:8000/health                      # {"ok":true,"version":"2.2.0",...}

# 4. API is protected
curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/issues                              # 401
KEY=$(sudo sed -n 's/^SRE_API_KEY=\([^ ]*\).*/\1/p' /etc/sre-agent/sre-agent.env)
curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: $KEY" localhost:8000/issues         # 200

# 5. The watcher started
sudo grep -m1 "auto mode ON" /var/log/sre-agent/sre.log          # a line with "auto.01.start"
```

### End-to-end test: make a test incident

Write one test error line into the watched log (same-server log; for SSH, run it on the log server). Use **your**
service name and log path:

```bash
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [ERROR] [orders-api] SreAgentTest: this is a test error, please ignore" \
  | sudo tee -a /var/log/orders-api/error.log
```

Within a few seconds:

```bash
curl -s -H "X-API-Key: $KEY" localhost:8000/issues | head -c 400
# {"issues":[{"id":"...","service":"orders-api","error":"SreAgentTest","severity":"HIGH","state":"open","count":1,...
```

and an **incident card** arrives in Slack / Google Chat / email with the root-cause summary and **Approve / Reject /
Request changes** buttons. Click **Reject** for the test.

If the issue appears but no message arrives, see Troubleshooting ("No Slack/email message").

### Checklist

- [ ] `sre-agent doctor` shows `all required checks passed`
- [ ] `/health` returns `"ok":true`
- [ ] `/issues` returns 401 without the key and 200 with it
- [ ] `auto mode ON` is in `/var/log/sre-agent/sre.log`
- [ ] The test error became an issue and a message arrived
- [ ] After a reboot the agent is running again (`systemctl status sre-agent` or `sudo pm2 list`)

---

## Step 7. Day-to-day operations

| Task | systemd | pm2 |
|---|---|---|
| Status | `sudo systemctl status sre-agent` | `sudo pm2 list` |
| Live output | `journalctl -u sre-agent -f` | `sudo pm2 logs sre-agent` |
| Agent event log | `sudo tail -f /var/log/sre-agent/sre.log` | same |
| Change settings | `sudo sre-agent setup --only notifications` (or `llm`, `logs`, `web`, `code`, `automation`), accept the restart | `sudo sre-agent setup --only ...`, answer `n` to start, then `sudo pm2 restart sre-agent` |
| Restart | `sudo systemctl restart sre-agent` | `sudo pm2 restart sre-agent` |
| Stop | `sudo systemctl stop sre-agent` | `sudo pm2 stop sre-agent` |
| Upgrade | `sudo apt install ./sre-agent_<new>-1_amd64.deb` (settings kept, DB backed up, restarted if running) | same, then `sudo pm2 restart sre-agent` |
| Back up | `sudo tar czf sre-agent-backup.tgz /etc/sre-agent /var/lib/sre-agent` | same |
| Remove | `sudo apt remove sre-agent` (keeps settings and data); `sudo apt purge sre-agent` also deletes settings | `sudo pm2 delete sre-agent && sudo pm2 save`, then the same |

Enable code fixes later: clone the repo for the service user, then rerun setup for `code` and `automation`:

```bash
sudo -u sre-agent git clone https://github.com/yourorg/orders-api.git /var/lib/sre-agent/repo
sudo sre-agent setup --only code,automation
```

---

## Upgrade an existing install (2.0.x / 2.1.x to 2.2.0)

Installing the new package on top of the old one keeps your settings, data and SSH key. The package stops the service,
saves the database to `/var/lib/sre-agent/backups/` (5 newest kept), replaces the program, leaves
`/etc/sre-agent/sre-agent.env` untouched and restarts the service if it was running.

1. **Check what you run:** `sre-agent version`; `sudo systemctl is-active sre-agent` (or `sudo pm2 list`).
2. **Safety copy (optional):** `sudo tar czf ~/sre-agent-backup-$(date +%F).tgz /etc/sre-agent /var/lib/sre-agent`
3. **Finish open approval cards.** Links and buttons sent by an older version stop working after the upgrade.
4. **Download and verify:**
   ```bash
   VERSION=2.2.0
   wget https://github.com/AA-Solutions-tech/SRE-Agent/releases/download/v$VERSION/sre-agent_$VERSION-1_amd64.deb
   wget https://github.com/AA-Solutions-tech/SRE-Agent/releases/download/v$VERSION/SHA256SUMS
   sha256sum -c --ignore-missing SHA256SUMS      # expect: ...: OK
   ```
5. **Install:** `sudo apt install ./sre-agent_$VERSION-1_amd64.deb` (RHEL: `sudo dnf upgrade <rpm url>`). With pm2, then
   `sudo pm2 restart sre-agent`.
6. **Check:** `sre-agent version` (2.2.0), `sudo sre-agent doctor` (read every WARN), `curl -s localhost:8000/health`.
7. **Prove monitoring works:** write one test error line to the watched log (Step 6) and reject the test card.

| Change | What you will notice | Old behaviour |
|---|---|---|
| Model calls | No custom `temperature` is sent, so real AI analysis runs | `LLM_TEMPERATURE=0.2` |
| Fallback cards | Start with "AI ANALYSIS UNAVAILABLE" and the reason | - |
| Severity | Inbound 4xx, bot probes, expected CORS = LOW (daily digest, no card); rate limit / external / bad data / security = MEDIUM | `SEVERITY_DEFAULTS=false` |
| Grouping | File names, paths, quoted values, hosts ignored when grouping | `FINGERPRINT_STRICT=true` |
| First read of a log | Watch mode starts at the log's end | `MONITOR_REPLAY_BACKLOG=true` |
| Cards | New "Ignore similar" button | - |
| Approval links | Confirm page first; single action; expire after 7 days | - |
| Agent log | Idle polls no longer printed | `SRE_TRACE_DEBUG=true` |

**Roll back:** `sudo apt install --allow-downgrades ./sre-agent_2.0.0-1_amd64.deb`, stop the service, copy the wanted file from
`/var/lib/sre-agent/backups/` over `/var/lib/sre-agent/starter.db` (as `sre-agent`), start the service.
**No automatic updates:** the packages are not in an apt/dnf repository; download each release yourself.

## New settings in 2.1 and 2.2 (all optional)

`MONITOR_LOG_PATH` (several logs: `/var/log/a.log@svc-a, /var/log/b.log@svc-b`), `IGNORE_PATTERNS` / `IGNORE_FILE`,
`SEVERITY_RULES_FILE` / `SERVICE_SEVERITY`, `LOW_DIGEST_HOURS` / `LOW_ESCALATE_COUNT`, `SLACK_BOT_TOKEN` /
`SLACK_SIGNING_SECRET` / `SLACK_ALLOWED_USERS` (Slack in-app buttons; Interactivity URL `https://<address>/slack/interactions`),
`APPROVAL_LINK_HOURS`, `ESCALATE_AFTER_HOURS`, `LLM_SELFTEST_ON_START`. Edit `/etc/sre-agent/sre-agent.env`, then
`sudo systemctl restart sre-agent` and run `sudo sre-agent doctor`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `doctor`: `[FAIL] APPROVAL_SECRET` | Secret missing or default | `sudo sre-agent setup` regenerates it |
| Service will not start: `refusing to start: another sre-agent is already running` | Both systemd and pm2 are running it | Keep one: `sudo systemctl disable --now sre-agent` **or** `sudo pm2 delete sre-agent` |
| Test error line does not create an issue | Auto mode is off | `sudo sre-agent setup --only automation`, answer yes to "Watch the logs continuously" |
| Test error line does not create an issue | Line format not recognised | Use one of the formats in Step 0 |
| Test error line does not create an issue | `sre-agent` user cannot read the log | Step 3; test with `sudo -u sre-agent head -1 <log>` |
| Works by hand but not under systemd for a log in `/home` | systemd hides `/home` | Step 3: `ProtectHome=read-only` drop-in |
| No Slack/email message, but the issue exists | Wrong webhook / SMTP details | `sudo sre-agent setup --only notifications`; check `sudo grep -i notify /var/log/sre-agent/sre.log` |
| `doctor`: `[FAIL] SSH log source` / `SSH reachable` | Key not authorised, host key not trusted, or firewall | Step 4.3; test with `sudo -u sre-agent ssh -i /var/lib/sre-agent/.ssh/id_ed25519 logreader@10.0.1.25 true` |
| `doctor`: `[WARN] TEST_CMD 'npm' not on PATH` | Node installed for another user (nvm) | Install node system-wide, or `sudo systemctl edit sre-agent` → `[Service]` `Environment=PATH=/path/to/node/bin:/usr/local/bin:/usr/bin:/bin` |
| Approve / Reject links do not open | Public URL is `localhost` | Put nginx/Caddy with HTTPS in front and set `sudo sre-agent setup --only web` |

Get help: send the output of `sudo sre-agent doctor` and the last 100 lines of `/var/log/sre-agent/sre.log`
(the agent masks tokens in its log, but glance through it before sending).

# Monitor agent

Tails a log source, files ERROR/WARN as signals, audits everything else.

## Run

```bash
python run.py monitor --mode once --source sre_agent/sre/monitor/fixtures/mixed.log
python run.py monitor --mode watch --source data/app.log
```

`--source`: file path | `local_file` (env `MONITOR_LOG_PATH`) | `ssh` | `docker:<container>` | `journald:<unit>`
(`MONITOR_LOG_PATH` may list several, comma separated: one watcher each)
(remote log via `core.ssh_logs`). `--mode`: `once` (tests/cron) | `watch`
(poll every `POLL_SECONDS`, stop with Ctrl+C).

## Rules

- ERROR/WARN → signal (one stored item per run, all lines as evidence).
- INFO/DEBUG → audit only, never a signal, never a notification.
- Known operational errors (missing env key, bad credentials, rate limit, bad recipient, server, external) are
  tagged with a `category` here (`sre/classify.py`); the incident agent then sends an "action needed" card instead
  of a code-fix approval. Rate-limit/external errors are only a card once they persist (`WATCH_THRESHOLD`).
- Every polled line is also shown to `sre/schedules.py`, which alerts when a configured cron job did not run.
- Unparsable lines → quarantined (attached as context, never paging alone).
- The monitor only notifies (`core.notify`, console fallback until
  `SLACK_WEBHOOK_URL` is set). It never requests approval or changes anything.

## Handoff

Each firing stores `create_item("signal", {service, severity, error,
evidence[], ts, source})`. The investigate agent reads that record by id —
see the frozen contract in `parse.build_signal`.

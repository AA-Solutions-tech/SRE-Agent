# sre/ — the SRE loop in one folder

Every SRE agent, the supervisor, and the SRE HTTP endpoints live here.
Kit demos (`agents/hello`, `agents/manager_report`) stay outside, untouched.

```
sre/
  monitor/      watch logs 24/7 -> file signals (JSON records)
  incident/     RCA a signal -> Slack summary card + Approve/Reject gate
  coding/       (next) fix approved work -> PR
  supervisor/   hub: intake -> route (Clef, rules fallback) -> delegate -> notify
  endpoints.py  GET /agents, GET|POST /signals, GET /signals/{id}
```

## The loop

```
monitor --(signal JSON record)--> supervisor --(route)--> incident
    --> Slack summary --> human approves --> supervisor --> coding
    --> fix --> supervisor --> monitor re-checks --> clear? PR : again
```

## Conventions (see RULES.md at repo root)

- Folder name = routing identity = CLI/API name (`monitor`, `incident`,
  `coding`, `supervisor`). Friendly labels ("investigate") only in docs/cards.
- Agents exchange JSON records by id (`signal`, `investigation`, `fix`).
  Humans get rendered summaries (Slack cards, PR text) — never raw JSON.
- Sub-agents are called via public `run()` only (`load_agent`), never imported.
- Deleting a folder shrinks the system: discovery, router, `/agents` follow.

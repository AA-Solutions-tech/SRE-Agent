# Supervisor agent

Routes one SRE signal to the best installed agent, then reports back.

```
intake (signal_id | injected event) -> route (Clef, rules fallback)
  -> delegate (sub-agent run() only) -> notify
```

## Run it

```bash
python run.py supervisor --signal-id <id>
python run.py supervisor --event '{"service":"api","severity":"HIGH","error":"boom","source":"/tmp/x.log"}'
```

Over HTTP: `POST /run/supervisor {"signal_id": "..."}` or `{"event": {...}}`.

## Routing

`SUPERVISOR_ROUTER=clef` (default, Cloudflare Workers AI model from
`CLEF_MODEL`) or `rules` (offline deterministic fallback). Clef answers
`{agent, confidence, reason}`; below `CLEF_MIN_CONFIDENCE`, or on any
error/unconfigured credentials, the rules router takes over and the
fallback is named in the result. Either way the decision is constrained
to installed agents — a removed agent is never routable.

## Removal matrix

| Missing agent | Supervisor behaviour |
|---|---|
| monitor | runs from injected `--event` payloads |
| incident / coding | routes to whatever capable agent remains; `None` -> clear "not installed" notice |
| (any) | `run.py <agent>` / `POST /run/<agent>` for the rest keep working untouched |

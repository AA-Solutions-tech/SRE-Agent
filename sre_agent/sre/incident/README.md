# Investigate agent (`incident`)

RCA a monitor signal, post a human summary card to Slack, gate the fix
behind Approve/Reject. Approving queues the fix for the coding agent —
this agent never changes code.

```
load (signal JSON) -> gather (recurrence) -> rca (LLM, heuristic fallback)
  -> card (Slack summary + act gate)
```

## Run it

```bash
python run.py incident --signal-id <id>
```

Over HTTP: `POST /run/incident {"signal_id": "..."}`. Approval clicks:
`GET /approve/<investigation-id>/act/approve|reject?token=…` (signed links
on the Slack card; server must be public via ngrok + `APP_BASE_URL` for
real phone clicks).

## Contract

- In: signal record id (the monitor's frozen JSON contract).
- Out: `investigation` record `{signal_id, service, severity, error, rca}`,
  status `awaiting_incident_fix` → `incident_fix_approved` / `incident_fix_rejectd`
  (kit convention: stage + action + "d").
- Slack users get the rendered markdown summary — never raw JSON.
- No LLM key / LLM error → deterministic heuristic RCA (offline-safe).

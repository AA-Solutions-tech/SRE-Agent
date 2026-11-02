# Coding agent (Agent 3)

Implements an **approved** investigation fix in an isolated worktree, runs tests, returns JSON to the Supervisor.

## Flow (Supervisor routes — this agent never routes)

```text
Supervisor --(incident_id, signal, investigation) JSON--> coding.run()
  --> validate -> setup -> context -> fix -> verify -> report
  --CodingResult JSON--> Supervisor --VerifyTask--> Monitor --> Supervisor
```

Slack summaries are derived from the JSON; agents never parse Slack text.

## Run

```bash
python run.py coding --incident-id INC1   # signal/investigation resolved via DB when IDs passed
python test-scripts/demo_coding.py        # offline end-to-end on a temp repo
```

## Safety

* Works ONLY in `WORKTREES_DIR/<incident_id>` on branch `<BRANCH_PREFIX>/<incident_id>`.
* Never touches `REPO_DIR` directly; never commits/pushes/merges/deploys (MVP).
* LLM proposes content; plain code applies via `safe_path`. No arbitrary shell — tests run only via `TEST_CMD`.
* `blocked` on missing/low-confidence reports; `failed` honestly on test failure; high-risk diffs set `requires_approval=True`.

## Config (`CODING_*`, see `.env.example`)

`CODING_MAX_FIX_ITERS, CODING_LLM_RETRIES, CODING_TEST_TIMEOUT, CODING_MIN_CONFIDENCE, CODING_ALLOWED_PATHS, VERIFY_WINDOW_SECONDS, MAX_AUTO_ROUNDS`

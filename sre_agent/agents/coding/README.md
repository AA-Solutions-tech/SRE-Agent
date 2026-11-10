# Coding agent (Agent 3)

Implements an **approved** investigation fix in an isolated worktree, runs tests, returns JSON to the Supervisor.

## Flow (Supervisor routes — this agent never routes)

```text
Supervisor --(incident_id, signal, investigation) JSON--> coding.run()
  --> validate -> setup -> context -> fix -> add_tests -> verify -> review -> report
      (a failed check or a failed review sends the work back to fix, bounded by CODING_FIX_ROUNDS)
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
* Lint/test commands run with a scrubbed environment (no API keys, tokens or DB URLs; `CODING_TEST_ENV_ALLOW` adds
  names) and, on Linux with `CODING_TEST_NO_NETWORK=true`, with no network. This limits what AI-written code can
  reach; it is not a full sandbox.
* A run that produces no fix removes its worktree and branch (the diff it tried is kept on the `coding_result`
  record); the failure goes to `sre/fixfail.py` (card: Retry the fix / I'll fix this / Ignore).
* A fix is held for human approval when no tests ran, the diff is high-risk, or the self-review had concerns.
* `blocked` on missing/low-confidence reports; `failed` honestly on test failure; high-risk diffs set `requires_approval=True`.

## Config (`CODING_*`, see `.env.example`)

`CODING_MAX_FIX_ITERS, CODING_LLM_RETRIES, CODING_TEST_TIMEOUT, CODING_MIN_CONFIDENCE, CODING_ALLOWED_PATHS, VERIFY_WINDOW_SECONDS, MAX_AUTO_ROUNDS`

## Extra steps
* `add_tests` (CODING_WRITE_TESTS): the model proposes NEW test file(s); code checks the path, size and syntax, and
  keeps them only if TEST_CMD still passes (otherwise they are dropped and recorded as a risk).
* `review` (CODING_SELF_REVIEW): a second model call reviews the diff against the root cause; a fail goes back to
  fix once more, a repeat fail is listed as a risk and holds the change.
* The investigation's `context_files` are shown to the model read-only, so it can see how the code is used.

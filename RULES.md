# Rules & Regulations — SRE-agent project

Working agreements for anyone (human or agent) building in this repo.
Violate these only with explicit owner permission.

## 1. Git discipline (strict)

- **NEVER run git write commands without permission.** This includes
  `add`, `commit`, `push`, `merge`, `rebase`, `remote add/rename/set-url`,
  `gh repo create`, branch deletes, and anything that changes history,
  branches, remotes, or the GitHub repo.
- **Read-only git is always fine:** `status`, `log`, `diff`, `branch -a`,
  `remote -v`. Inspect freely; change nothing.
- Commit only when the owner says so (e.g. "commit this"). Push only when
  the owner says so. No exceptions for "small" or "obvious" commits.

## 2. Scope boundaries

- Work **only** in this repo (`SRE-agent/`) on **`main`**, unless told otherwise.
- **Never touch:** the `Wave/` folder, the `hackathon-Stater-kit/` leftover copy,
  the parent `sre-agent/` repo, or the starter-kit template remote.
- **Never push to the template** (`hackathon-Stater-kit.git`). Our only push
  remote is `AA-Solutions-tech/SRE-Agent`.

## 3. Secrets & local state

- `.env`, `.venv/`, `data/` (SQLite), and `__pycache__/` stay local —
  all gitignored. Never commit them, never paste tokens into chat or code.
- Tokens/keys (Clef, Slack, GitHub) travel via environment variables and
  `.env` only. Example values go in `.env.example`, never real ones.

## 4. Agent architecture

- One agent = one folder under `agents/` exposing `build()` + `run()`.
  Auto-discovery (`run.py`, `server.py`) picks it up — no framework edits.
- Agents must run **individually**: deleting a folder only shrinks the
  system, never breaks the rest.
- The supervisor routes **only to installed agents** (enforced in code,
  never trusted to the model) and falls back to the rules router whenever
  Clef is unreachable, unconfigured, or unsure.
- Sub-agents are called via public `run()` only — never their internals.

## 5. Verification before "done"

- Full test suite green (`pytest tests/`) before declaring any work finished.
- HTTP claims proven with a real boot + `curl` (`/health`, `/run/<agent>`),
  not assumed from code reading.
- No leftover background processes (servers, watchers) when finished.

## 6. Simplicity

- Build only the required agents and functions. Serve only what is needed.
- No new endpoints, dashboards, auth schemes, or dependencies without
  owner approval. Propose first, build after.

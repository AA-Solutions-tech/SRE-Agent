"""Live demo: the full SRE loop on a synthetic log.

Run from the repo root with the project venv::

    python test-scripts/demo_loop.py

What it shows:
  1. Monitor watches a log file and files a signal (JSON record).
  2. The stored signal JSON (the handoff package between agents).
  3. Supervisor routes HIGH -> incident; incident files an RCA + Slack card.
  4. Human approves the gate -> fix queued for the coding agent.
  5. Monitor re-checks a clean log -> quiet (the post-fix verify leg).

Uses an isolated temp SQLite DB, so the real data/starter.db is untouched.
Notifications print to console (no Slack webhook configured).
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["SUPERVISOR_ROUTER"] = "rules"  # deterministic demo routing

from sre_agent.core.config import settings  # noqa: E402

# Isolated DB for the demo (frozen dataclass -> object.__setattr__).
_tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", str(Path(_tmp.name) / "demo.db"))

from sre_agent.core.approvals import handle_decision  # noqa: E402
from sre_agent.core.db import get_item, list_items  # noqa: E402
from sre_agent.core.notify import approval_token  # noqa: E402
from sre_agent.sre.incident.agent import STAGE  # noqa: E402
from sre_agent.sre.monitor.agent import run as monitor_run  # noqa: E402
from sre_agent.sre.supervisor.agent import run as supervise  # noqa: E402


def step(title):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def main():
    log = Path(_tmp.name) / "demo.log"
    log.write_text(
        "[2026-10-09T11:00:01Z] [INFO] [payment-api] request ok\n"
        "[2026-10-09T11:00:02Z] [ERROR] [payment-api] MongoNetworkTimeout after 3 retries\n"
        "[2026-10-09T11:00:03Z] [ERROR] [payment-api] checkout failed for order #8812\n"
    )

    step("1. MONITOR watches the log")
    out = monitor_run(source=str(log), mode="once")
    print("monitor says:", out)
    signal_id = out.split()[1].rstrip(":")
    print("signal id:", signal_id)

    step("2. THE SIGNAL (JSON handoff between agents)")
    print(json.dumps(get_item(signal_id), indent=1)[:1200])

    step("3. SUPERVISOR routes -> INCIDENT investigates (Slack card below)")
    print("supervisor says:", supervise(signal_id=signal_id))

    step("4. HUMAN approves the fix gate")
    inv = list_items(kind="investigation")[0]
    print("investigation:", inv["id"], "| status:", inv["status"])
    msg = handle_decision(inv["id"], STAGE, "approve", approval_token(inv["id"], STAGE))
    print("gate says:", msg, "| new status:", get_item(inv["id"])["status"])

    step("5. MONITOR re-checks the (now clean) log")
    log.write_text("[2026-10-09T12:00:01Z] [INFO] [payment-api] all good\n")
    print("monitor says:", monitor_run(source=str(log), mode="once"))

    step("DONE: monitor -> supervisor -> incident -> approve -> re-verify")


if __name__ == "__main__":
    main()

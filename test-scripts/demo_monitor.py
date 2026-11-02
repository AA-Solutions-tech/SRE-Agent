"""Live demo: monitor agent end-to-end on a synthetic log.

Run from the repo root with the project venv::

    python test-scripts/demo_monitor.py

What it shows:
  1. INFO lines are audited silently, ERROR lines fire one signal.
  2. The stored signal record (the handoff package for the investigate agent).
  3. Appended WARN fires a second, separate signal; repeats stay quiet.
  4. Garbage lines never page.

Uses an isolated temp SQLite DB, so the real data/starter.db is untouched.
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sre_agent.core.config import settings  # noqa: E402

# Isolated DB for the demo (frozen dataclass -> object.__setattr__).
_tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", str(Path(_tmp.name) / "demo.db"))

from sre_agent.sre.monitor.agent import run  # noqa: E402
from sre_agent.core.db import list_items  # noqa: E402


def step(title):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def main():
    log = Path(_tmp.name) / "demo.log"
    log.write_text(
        "[2026-10-09T12:00:01Z] [INFO] [payment-api] request completed in 12ms\n"
        "[2026-10-09T12:00:02Z] [INFO] [payment-api] request completed in 9ms\n"
        "[2026-10-09T12:00:03Z] [ERROR] [payment-api] MongoNetworkTimeout after 3 retries\n"
        "[2026-10-09T12:00:04Z] [INFO] [payment-api] health check ok\n"
        "[2026-10-09T12:00:05Z] [ERROR] [payment-api] checkout failed for order #8812\n"
    )

    step("1. First run on a log with 2 ERRORs (expect: 1 signal)")
    print(run(source=str(log), mode="once"))

    step("2. Stored signal record (what the investigate agent receives)")
    print(json.dumps(list_items(kind="signal")[0]["data"], indent=1))

    step("3. Append a WARN and re-run (expect: 2nd signal, MEDIUM)")
    with open(log, "a") as f:
        f.write(
            "[2026-10-09T12:00:06Z] [WARN] [payment-api] retry budget 80% consumed\n"
        )
    print(run(source=str(log), mode="once"))

    step("4. Re-run with nothing new (expect: quiet)")
    print(run(source=str(log), mode="once"))

    step("5. Append garbage and re-run (expect: quiet, quarantined)")
    with open(log, "a") as f:
        f.write("this is not a log line\n")
    print(run(source=str(log), mode="once"))

    step(f"Done: {len(list_items(kind='signal'))} signals stored, "
         f"{len(list_items(kind='monitor_cursor'))} cursor(s) tracked")


if __name__ == "__main__":
    main()

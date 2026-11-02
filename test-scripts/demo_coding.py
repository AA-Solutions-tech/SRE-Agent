"""Live demo: coding agent end-to-end on a synthetic repo (offline, temp DB).

Run from the repo root with the project venv::

    python test-scripts/demo_coding.py

Shows: blocked on bad input -> completed fix on good input -> stored CodingResult.
Uses an isolated temp SQLite DB + temp git repo; real data/ is untouched.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sre_agent.core.config import settings  # noqa: E402

_tmp = tempfile.TemporaryDirectory()
object.__setattr__(settings, "db_path", str(Path(_tmp.name) / "demo.db"))


def _sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def main():
    repo = Path(_tmp.name) / "repo"
    repo.mkdir()
    _sh(repo, "git", "init", "-b", "main")
    _sh(repo, "git", "config", "user.email", "t@t")
    _sh(repo, "git", "config", "user.name", "t")
    (repo / "app.py").write_text("def total(a, b):\n    return a - b\n")
    _sh(repo, "git", "add", "app.py")
    _sh(repo, "git", "commit", "-m", "init")
    object.__setattr__(settings, "repo_dir", repo)
    object.__setattr__(settings, "worktrees_dir", Path(_tmp.name) / "wt")
    object.__setattr__(settings, "base_branch", "main")
    object.__setattr__(settings, "test_cmd", "python --version")

    from sre_agent.agents.coding import agent as coding  # noqa: E402

    class FakeLLM:
        def invoke(self, prompt):
            class M:
                content = "def total(a, b):\n    return a + b\n"
            return M()

    coding.get_llm = lambda: FakeLLM()  # offline demo, no API key needed
    coding.notify = lambda *a, **k: print(f"[notify] {a[0] if a else ''}")

    signal = {"service": "demo", "severity": "HIGH", "error": "AssertionError",
              "evidence": ["total(2,3)==-1"], "ts": "2026-10-09T00:00:00Z", "source": "demo"}
    print("1. Bad input (expect blocked):")
    print(json.dumps(coding.run(incident_id="BAD", signal=signal, investigation={"root_cause": "x"}), indent=1))

    inv = {"incident_id": "INC1", "root_cause": "total() subtracts instead of adding",
           "evidence": ["AssertionError"], "affected_files": ["app.py"],
           "remediation": "change `return a - b` to `return a + b`",
           "confidence": 0.9, "required_tests": ["existing suite"]}
    print("\n2. Good input (expect completed):")
    print(json.dumps(coding.run(incident_id="INC1", signal=signal, investigation=inv), indent=1))


if __name__ == "__main__":
    main()

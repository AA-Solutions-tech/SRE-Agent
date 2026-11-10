"""Regression tests for a fix: the model proposes NEW test files, plain code validates and writes them.

The patch step edits only the files the investigation named, so without this step a fix ships with no test
that would have caught the bug. Here the model gets the root cause, the diff, the names of the project's
existing tests and one sample (for style), and returns {"tests": [{"file", "content"}]}.

Rules enforced in code, not trusted to the model:
  * the path must look like a test file (test_*.py, *.test.ts, tests/ ...) and resolve inside the worktree;
  * only NEW files (an existing test is never overwritten or edited here);
  * size cap, and a syntax check (a file that does not parse is dropped);
  * the caller runs TEST_CMD with the new tests and removes them again if the suite does not pass.
"""
import json
import re
from pathlib import Path

from sre_agent.core.git_tools import _git, safe_path

from . import patch

MAX_TEST_FILES = 2
MAX_TEST_CHARS = 20_000
TEST_NAME_RE = re.compile(r"(^|/)(test_[^/]*\.py|[^/]*_test\.(?:py|go)|[^/]*\.(?:test|spec)\.[cm]?[jt]sx?|[^/]*Test\.java)$")
TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs"}
NL = chr(10)

SYSTEM_TESTS = """You write a minimal regression test for a bug fix, in the project's own test style.

Reply with ONE JSON object and nothing else:
{"tests": [{"file": "<new test file path>", "content": "<full file content>"}]}

Rules:
- The test must FAIL on the old code and PASS on the fixed code: exercise exactly the case in the root cause.
- Create a NEW file (never an existing one). Put it where the project's other tests live and name it the same way.
- Use only the test framework and helpers the sample test already uses. No new dependencies, no network, no real
  secrets, no sleeping. Keep it short and deterministic.
- If you cannot write a reliable test from what you were given, return {"tests": []}.
"""


def is_test_path(rel: str) -> bool:
    p = rel.replace("\\", "/").lstrip("./")
    return bool(TEST_NAME_RE.search(p)) or bool(TEST_DIRS & set(p.split("/")[:-1]))


def existing_tests(wt: Path, limit: int = 15) -> list[str]:
    out = _git(wt, "ls-files", check=False)
    return [p for p in out.splitlines() if is_test_path(p)][:limit]


def _sample(wt: Path, paths: list[str]) -> str:
    for rel in paths:
        try:
            text = safe_path(wt, rel).read_text(encoding="utf-8", errors="replace")
            return f"### {rel}{NL}{text[:3000]}"
        except (OSError, ValueError):
            continue
    return "(the project has no tests to copy the style from)"


def build_prompt(inv: dict, diff_text: str, tests: list[str], sample: str) -> str:
    return NL.join([
        f"Root cause: {inv.get('root_cause', '')}",
        f"Remediation: {inv.get('remediation', '')}",
        f"Tests the plan asked for: {inv.get('required_tests', [])}",
        "", "EXISTING TEST FILES (for naming and location):", *(tests or ["(none found)"]),
        "", "SAMPLE TEST (copy its style):", sample,
        "", "THE FIX (diff):", diff_text[:8000],
        "", "Return the JSON now."])


def parse_tests(reply: str) -> list[dict]:
    m = re.search(r"\{.*\}", reply, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in model reply")
    out = []
    for t in (json.loads(m.group(0)).get("tests") or [])[:MAX_TEST_FILES]:
        if isinstance(t, dict) and isinstance(t.get("file"), str) and isinstance(t.get("content"), str):
            out.append({"file": t["file"].strip(), "content": t["content"]})
    return out


def add_regression_tests(wt: Path, inv: dict, diff_text: str, ask) -> dict:
    """Ask for and write new test files. Returns {"files": [rel paths written], "notes": [why anything was dropped]}."""
    tests = existing_tests(wt)
    proposed = parse_tests(ask(SYSTEM_TESTS + NL + NL + build_prompt(inv, diff_text, tests, _sample(wt, tests))))
    files, notes = [], []
    for t in proposed:
        rel = t["file"]
        try:
            target = safe_path(wt, rel)
        except ValueError as e:
            notes.append(f"{rel}: {e}")
            continue
        if not is_test_path(rel):
            notes.append(f"{rel}: not a test file path, dropped")
        elif target.exists():
            notes.append(f"{rel}: already exists, an existing test is never overwritten")
        elif not t["content"].strip() or len(t["content"]) > MAX_TEST_CHARS:
            notes.append(f"{rel}: empty or larger than {MAX_TEST_CHARS} chars, dropped")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(t["content"].encode("utf-8", patch.ENC))
            check = patch.syntax_check(target, wt)
            if check is not None and not check["passed"]:
                target.unlink()
                notes.append(f"{rel}: dropped, syntax check failed: {check['output_tail'][:150]}")
            else:
                files.append(rel)
    return {"files": files, "notes": notes}


def remove(wt: Path, files: list[str]) -> None:
    for rel in files:
        try:
            safe_path(wt, rel).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

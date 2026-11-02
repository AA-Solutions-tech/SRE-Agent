"""Coding agent tests (offline: FakeLLM, temp git repo, isolated SQLite DB)."""
import json
import subprocess

import pytest

from sre_agent.agents.coding import agent as coding
from sre_agent.agents.coding import validate as v
from sre_agent.core import git_tools as g
from sre_agent.core.db import list_items


def _sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, cfg):
    r = tmp_path / "repo"
    r.mkdir()
    _sh(r, "git", "init", "-b", "main")
    _sh(r, "git", "config", "user.email", "t@t")
    _sh(r, "git", "config", "user.name", "t")
    (r / "app.py").write_text("def total(a, b):\n    return a - b\n")
    _sh(r, "git", "add", "app.py")
    _sh(r, "git", "commit", "-m", "init")
    cfg(repo_dir=r, worktrees_dir=tmp_path / "wt", base_branch="main",
        dry_run_push=True, test_cmd="", coding_min_confidence=0.5,
        coding_allowed_paths=(), coding_max_fix_iters=1, coding_llm_retries=1)
    return r


def _inv(**kw):
    base = {
        "incident_id": "INC1",
        "root_cause": "total() subtracts instead of adding",
        "evidence": ["AssertionError: total(2,3)==-1"],
        "affected_files": ["app.py"],
        "remediation": "change `return a - b` to `return a + b`",
        "confidence": 0.9,
        "required_tests": ["existing suite"],
    }
    base.update(kw)
    return base


def _sig():
    return {"service": "demo", "severity": "HIGH", "error": "AssertionError",
            "evidence": ["boom"], "ts": "2026-10-09T00:00:00Z", "source": "test"}


_EDIT = json.dumps({"edits": [{"file": "app.py", "search": "return a - b", "replace": "return a + b",
                               "reason": "add instead of subtract"}]})


class _FakeLLM:
    def __init__(self, text):
        self.text = text

    def invoke(self, prompt):
        class M:
            content = self.text
        return M()


def test_validate_blocks_on_missing_fields(cfg):
    cfg(coding_min_confidence=0.5, coding_allowed_paths=())
    ok, details = v.validate("INC1", _sig(), {"root_cause": "x"})
    assert not ok and "clarification_needed" in details


def test_validate_rejects_blocked_paths(cfg):
    cfg(coding_min_confidence=0.0, coding_allowed_paths=())
    ok, details = v.validate("INC1", _sig(), _inv(affected_files=[".env"]))
    assert not ok
    ok, _ = v.validate("INC1", _sig(), _inv(affected_files=["../evil.py"]))
    assert not ok


def test_validate_rejects_low_confidence(cfg):
    cfg(coding_min_confidence=0.5, coding_allowed_paths=())
    ok, details = v.validate("INC1", _sig(), _inv(confidence=0.1))
    assert not ok and any("confidence" in c for c in details["clarification_needed"])


def test_blocked_returns_no_worktree(monkeypatch, cfg):
    monkeypatch.setattr(coding, "notify", lambda *a, **k: None)
    out = coding.run(incident_id="INC9", signal=_sig(), investigation={"root_cause": "x"})
    assert out["status"] == "blocked" and out["worktree"] == ""
    assert list_items(kind="coding_result")


def test_happy_path_writes_and_tests_pass(repo, monkeypatch, cfg):
    cfg(test_cmd="python --version")
    monkeypatch.setattr(coding, "get_llm", lambda: _FakeLLM(_EDIT))
    monkeypatch.setattr(coding, "notify", lambda *a, **k: None)
    out = coding.run(incident_id="INC1", signal=_sig(), investigation=_inv())
    assert out["status"] == "completed"
    assert out["changed_files"] and out["validation"]["tests_passed"] is True
    assert (repo / "app.py").read_text().startswith("def total")  # base untouched
    assert "a + b" in (list(repo.parent.glob("wt/INC1/app.py"))[0].read_text())


def test_test_failure_reports_honestly(repo, monkeypatch, cfg):
    cfg(test_cmd="python -c \"import sys; sys.exit(1)\"")
    monkeypatch.setattr(coding, "get_llm", lambda: _FakeLLM(_EDIT))
    monkeypatch.setattr(coding, "notify", lambda *a, **k: None)
    out = coding.run(incident_id="INC2", signal=_sig(), investigation=_inv())
    assert out["status"] == "failed" and out["requires_approval"] is True
    assert out["tests"] and out["tests"][0]["passed"] is False


def test_branch_collision_fails_safe(repo, monkeypatch, cfg):
    g.create_worktree("DUP")
    monkeypatch.setattr(coding, "notify", lambda *a, **k: None)
    out = coding.run(incident_id="DUP", signal=_sig(), investigation=_inv())
    assert out["status"] == "failed" and "worktree" in out["failure_reason"].lower()


def test_discovered_by_runner():
    from sre_agent.agents import agent_names, load_agent

    assert "coding" in agent_names()
    assert hasattr(load_agent("coding"), "run")


# ---- fix_type gate inside the coding agent ----
def test_validate_blocks_ops_fix_type(monkeypatch):
    from sre_agent.agents.coding.validate import validate

    inv = {"root_cause": "token expired", "remediation": "re-auth", "affected_files": ["a.js"],
           "evidence": ["x"], "confidence": 0.9, "required_tests": ["t"], "fix_type": "ops"}
    ok, details = validate("INC", {"service": "s"}, inv)
    assert not ok and "ops" in details["failure_reason"]
    monkeypatch.setenv("CODING_ALLOW_OPS", "true")
    assert validate("INC", {"service": "s"}, inv)[0]


def test_validate_blocks_files_the_reviewer_ruled_out():
    from sre_agent.agents.coding.validate import validate

    inv = {"root_cause": "x", "remediation": "y", "affected_files": ["apps/a.js", "apps/b.js"], "evidence": ["e"],
           "confidence": 0.9, "required_tests": ["t"], "constraints": {"deny_paths": ["apps/b.js"], "allow_paths": []}}
    ok, details = validate("INC", {"service": "s"}, inv)
    assert not ok and "reviewer said not to touch" in details["failure_reason"]
    inv["constraints"] = {"deny_paths": [], "allow_paths": ["apps/a.js"]}
    ok, details = validate("INC", {"service": "s"}, inv)
    assert not ok and "outside the folders the reviewer allowed" in details["failure_reason"]
    inv["constraints"] = {"deny_paths": [], "allow_paths": ["apps"]}
    assert validate("INC", {"service": "s"}, inv)[0]


# ---- checks that fail send the work back to fix, once ----
class _SeqLLM:
    """Answers by what the prompt is: first fix, sweep ("EDITS ALREADY APPLIED"), or retry ("THE CHECKS FAILED")."""

    def __init__(self, first, retry="{}", sweep='{"edits": []}'):
        self.first, self.retry, self.sweep, self.prompts = first, retry, sweep, []

    def __call__(self):
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        reply = (self.retry if "THE CHECKS FAILED" in prompt
                 else self.sweep if "EDITS ALREADY APPLIED" in prompt else self.first)

        class M:
            content = reply
        return M()


def _edit(search, replace):
    return json.dumps({"edits": [{"file": "app.py", "search": search, "replace": replace, "reason": "r"}]})


def test_failed_tests_go_back_to_fix_once_with_the_failure(repo, monkeypatch, cfg):
    marker = repo.parent / "marker.txt"  # the check passes only when the second attempt's code is in place
    cfg(test_cmd=f"python -c \"import sys; sys.exit(0 if '* b' in open('app.py').read() else 3)\"")
    llm = _SeqLLM(_edit("return a - b", "return a + b"), retry=_edit("return a + b", "return a * b"))
    monkeypatch.setattr(coding, "get_llm", llm)
    monkeypatch.setattr(coding, "notify", lambda *a, **k: None)
    out = coding.run(incident_id="RETRY1", signal=_sig(), investigation=_inv())
    assert out["status"] == "completed" and out["validation"]["tests_passed"] is True
    assert "* b" in list(repo.parent.glob("wt/RETRY1/app.py"))[0].read_text()
    retry_prompt = next(p for p in llm.prompts if "THE CHECKS FAILED" in p)
    assert "exited 3" in retry_prompt and "a + b" in retry_prompt  # it saw the failure AND its own first fix
    assert not marker.exists()


def test_a_retry_with_no_new_edit_stops_instead_of_looping(repo, monkeypatch, cfg):
    cfg(test_cmd="python -c \"import sys; sys.exit(1)\"")
    llm = _SeqLLM(_edit("return a - b", "return a + b"), retry=json.dumps({"edits": []}))
    monkeypatch.setattr(coding, "get_llm", llm)
    monkeypatch.setattr(coding, "notify", lambda *a, **k: None)
    out = coding.run(incident_id="RETRY2", signal=_sig(), investigation=_inv())
    assert out["status"] == "failed" and out["requires_approval"] is True
    assert sum("THE CHECKS FAILED" in p for p in llm.prompts) == 1  # one retry, then it stops


def test_the_round_limit_is_respected(repo, monkeypatch, cfg):
    monkeypatch.setenv("CODING_FIX_ROUNDS", "1")
    cfg(test_cmd="python -c \"import sys; sys.exit(1)\"")
    llm = _SeqLLM(_edit("return a - b", "return a + b"))
    monkeypatch.setattr(coding, "get_llm", llm)
    monkeypatch.setattr(coding, "notify", lambda *a, **k: None)
    out = coding.run(incident_id="RETRY3", signal=_sig(), investigation=_inv())
    assert out["status"] == "failed" and not any("THE CHECKS FAILED" in p for p in llm.prompts)


def test_lint_command_runs_before_tests_and_blocks_on_failure(repo, monkeypatch, cfg):
    monkeypatch.setenv("LINT_CMD", "python -c \"import sys; sys.exit(2)\"")
    monkeypatch.setenv("CODING_FIX_ROUNDS", "1")
    cfg(test_cmd="python --version")
    monkeypatch.setattr(coding, "get_llm", _SeqLLM(_edit("return a - b", "return a + b")))
    monkeypatch.setattr(coding, "notify", lambda *a, **k: None)
    out = coding.run(incident_id="LINT1", signal=_sig(), investigation=_inv())
    assert out["status"] == "failed" and out["tests"][0]["exit_code"] == 2

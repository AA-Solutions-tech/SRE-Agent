"""Coding agent: cleanup of failed runs, sandboxed checks, regression tests, self-review, read-only context. Offline."""
import json
import os
import subprocess

import pytest

from sre_agent.agents.coding import agent as coding
from sre_agent.agents.coding import review, tests_gen
from sre_agent.core import git_tools as g
from sre_agent.core.db import list_items


def _sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, cfg, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    _sh(r, "git", "init", "-b", "main")
    _sh(r, "git", "config", "user.email", "t@t")
    _sh(r, "git", "config", "user.name", "t")
    (r / "app.py").write_text("def total(a, b):\n    return a - b\n")
    (r / "util.py").write_text("def helper():\n    return 42\n")
    (r / "tests").mkdir()
    (r / "tests" / "test_util.py").write_text("from util import helper\n\n\ndef test_helper():\n    assert helper() == 42\n")
    _sh(r, "git", "add", ".")
    _sh(r, "git", "commit", "-m", "init")
    cfg(repo_dir=r, worktrees_dir=tmp_path / "wt", base_branch="main", dry_run_push=True, test_cmd="python --version",
        coding_min_confidence=0.5, coding_allowed_paths=(), coding_max_fix_iters=1, coding_llm_retries=1)
    monkeypatch.setattr(coding, "notify", lambda *a, **k: None)
    return r


def _inv(**kw):
    base = {"incident_id": "INC1", "root_cause": "total() subtracts instead of adding", "evidence": ["x"],
            "affected_files": ["app.py"], "remediation": "change `return a - b` to `return a + b`",
            "confidence": 0.9, "required_tests": ["total(2, 3) == 5"]}
    return {**base, **kw}


SIG = {"service": "demo", "severity": "HIGH", "error": "AssertionError", "evidence": ["boom"],
       "ts": "2026-10-09T00:00:00Z", "source": "test"}


def _edit(search="return a - b", replace="return a + b"):
    return json.dumps({"edits": [{"file": "app.py", "search": search, "replace": replace, "reason": "r"}]})


TEST_FILE = json.dumps({"tests": [{"file": "tests/test_total.py",
                                   "content": "from app import total\n\n\ndef test_total():\n    assert total(2, 3) == 5\n"}]})
PASS = '{"verdict": "pass", "problems": []}'
FAIL = '{"verdict": "fail", "problems": ["only one call site was fixed: the off by one remains in reports.py"]}'


class Router:
    """Answers by what the prompt is: tests, review, retry-after-review, sweep, or the first fix."""

    def __init__(self, edits=None, tests="", reviews=(), retry_edit=""):
        self.edits, self.tests, self.reviews, self.retry_edit = edits or _edit(), tests, list(reviews), retry_edit
        self.prompts = []

    def __call__(self):
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if "You write a minimal regression test" in prompt:
            reply = self.tests or '{"tests": []}'
        elif "strict senior code reviewer" in prompt:
            reply = self.reviews.pop(0) if self.reviews else PASS
        elif "EDITS ALREADY APPLIED" in prompt:
            reply = '{"edits": []}'
        elif "A REVIEWER REJECTED" in prompt and self.retry_edit:
            reply = self.retry_edit
        else:
            reply = self.edits

        class M:
            content = reply
        return M()


def _run(repo, monkeypatch, llm, incident="INC1", **inv):
    monkeypatch.setattr(coding, "get_llm", llm)
    return coding.run(incident_id=incident, signal=SIG, investigation=_inv(**inv))


# ---- gap 2: a run with no fix leaves nothing behind ----


def test_failed_checks_remove_the_work_area_and_branch_but_keep_the_diff(repo, monkeypatch, cfg, tmp_path):
    cfg(test_cmd="python -c \"import sys; sys.exit(1)\"")
    out = _run(repo, monkeypatch, Router())
    assert out["status"] == "failed" and out["worktree"] == ""
    assert not (tmp_path / "wt" / "INC1").exists() and not g.branch_exists("ai-fix/INC1")
    saved = [i for i in list_items(kind="coding_result") if i["data"]["status"] == "failed"][0]["data"]
    assert "a + b" in saved["failed_diff"]


def test_a_failed_run_can_be_retried_under_the_same_incident_id(repo, monkeypatch, cfg):
    cfg(test_cmd="python -c \"import sys; sys.exit(1)\"")
    assert _run(repo, monkeypatch, Router())["status"] == "failed"
    cfg(test_cmd="python --version")
    assert _run(repo, monkeypatch, Router())["status"] == "completed"  # no "branch already exists"


def test_no_change_made_is_cleaned_up_too(repo, monkeypatch, tmp_path):
    out = _run(repo, monkeypatch, Router(edits='{"edits": []}'))
    assert out["status"] == "failed" and not (tmp_path / "wt" / "INC1").exists() and not g.branch_exists("ai-fix/INC1")


def test_keep_failed_leaves_the_work_area_for_inspection(repo, monkeypatch, cfg, tmp_path):
    cfg(test_cmd="python -c \"import sys; sys.exit(1)\"")
    monkeypatch.setenv("CODING_KEEP_FAILED", "true")
    _run(repo, monkeypatch, Router())
    assert (tmp_path / "wt" / "INC1").is_dir()


def test_a_completed_run_keeps_its_work_area_until_it_is_shipped(repo, monkeypatch, tmp_path):
    out = _run(repo, monkeypatch, Router())
    assert out["status"] == "completed" and (tmp_path / "wt" / "INC1").is_dir()


def test_prune_stale_removes_old_work_areas_except_pending_ones(repo, tmp_path):
    for name in ("OLD1", "OLD2", "NEW1"):
        g.create_worktree(name)
    old = 86400 * 10
    for name in ("OLD1", "OLD2"):
        t = os.stat(tmp_path / "wt" / name).st_mtime - old
        os.utime(tmp_path / "wt" / name, (t, t))
    assert g.prune_stale(7, keep={"OLD2"}) == ["OLD1"]
    assert not (tmp_path / "wt" / "OLD1").exists() and (tmp_path / "wt" / "OLD2").exists()
    assert (tmp_path / "wt" / "NEW1").exists() and g.branch_exists("ai-fix/OLD1")  # branches are left alone


# ---- gap 5: checks run without the agent's secrets ----


def test_check_env_has_no_secrets_but_keeps_tool_paths(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "SMTP_PASSWORD", "DATABASE_URL", "GITHUB_TOKEN", "SLACK_BOT_TOKEN", "SRE_API_KEY"):
        monkeypatch.setenv(name, "secret-value")
    env = coding.check_env()
    assert not any("secret-value" == v for v in env.values()) and "PATH" in env and env["CI"] == "true"
    monkeypatch.setenv("CODING_TEST_ENV_ALLOW", "DATABASE_URL")
    assert coding.check_env()["DATABASE_URL"] == "secret-value"


def test_test_command_cannot_see_the_agents_keys(repo, monkeypatch, cfg):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live-should-not-leak")
    cfg(test_cmd="python -c \"import os, sys; sys.exit(1 if os.environ.get('ANTHROPIC_API_KEY') else 0)\"")
    assert _run(repo, monkeypatch, Router())["status"] == "completed"


def test_no_network_flag_falls_back_safely_off_linux(repo, monkeypatch):
    monkeypatch.setenv("CODING_TEST_NO_NETWORK", "true")
    monkeypatch.setattr(coding.sys, "platform", "win32")
    assert coding._no_network_argv("pytest") is None
    monkeypatch.setattr(coding.sys, "platform", "linux")
    monkeypatch.setattr(coding.shutil, "which", lambda n: "/usr/bin/unshare")
    assert coding._no_network_argv("pytest") == ["unshare", "-rn", "sh", "-c", "pytest"]


# ---- gap 4: strict mode ----


def test_require_tests_refuses_a_fix_with_no_test_command(repo, monkeypatch, cfg, tmp_path):
    cfg(test_cmd="")
    monkeypatch.setenv("CODING_REQUIRE_TESTS", "true")
    out = _run(repo, monkeypatch, Router())
    assert out["status"] == "failed" and "CODING_REQUIRE_TESTS" in out["failure_reason"]
    assert not (tmp_path / "wt" / "INC1").exists()


# ---- gap 3: regression tests ----


def test_a_regression_test_is_added_and_kept_when_the_suite_passes(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("CODING_WRITE_TESTS", "true")
    out = _run(repo, monkeypatch, Router(tests=TEST_FILE))
    assert out["status"] == "completed" and out["added_tests"] == ["tests/test_total.py"]
    assert (tmp_path / "wt" / "INC1" / "tests" / "test_total.py").is_file()
    assert "tests/test_total.py" in out["changed_files"] and "app.py" in out["changed_files"]


def test_a_generated_test_that_breaks_the_suite_is_dropped_not_fatal(repo, monkeypatch, cfg, tmp_path):
    monkeypatch.setenv("CODING_WRITE_TESTS", "true")
    cfg(test_cmd="python -c \"import os, sys; sys.exit(1 if os.path.exists('tests/test_total.py') else 0)\"")
    out = _run(repo, monkeypatch, Router(tests=TEST_FILE))
    assert out["status"] == "completed" and not out["added_tests"]
    assert not (tmp_path / "wt" / "INC1" / "tests" / "test_total.py").exists()
    assert any("failed validation and was dropped" in r for r in out["remaining_risks"])


def test_no_regression_test_is_attempted_without_a_test_command(repo, monkeypatch, cfg):
    monkeypatch.setenv("CODING_WRITE_TESTS", "true")
    cfg(test_cmd="")
    llm = Router(tests=TEST_FILE)
    _run(repo, monkeypatch, llm)
    assert not any("regression test" in p for p in llm.prompts)


def test_test_path_rules():
    for ok in ("tests/test_a.py", "src/foo.test.ts", "pkg/foo_test.go", "spec/models/user_spec.rb", "a/__tests__/x.js"):
        assert tests_gen.is_test_path(ok), ok
    for bad in ("app.py", "src/main.ts", "docs/readme.md", "config/settings.py"):
        assert not tests_gen.is_test_path(bad), bad


def test_generated_tests_are_validated_in_code(repo, tmp_path):
    wt, _ = g.create_worktree("T1")
    bad = [
        {"file": "app.py", "content": "print('overwrite')"},                      # not a test path
        {"file": "tests/test_util.py", "content": "def test_x(): pass"},           # exists: never overwritten
        {"file": "../outside/test_x.py", "content": "def test_x(): pass"},          # escapes the repo
        {"file": "tests/test_bad.py", "content": "def broken(:\n"},                 # does not parse
        {"file": "tests/test_empty.py", "content": "   "},
    ]
    for item in bad:
        reply = json.dumps({"tests": [item]})
        out = tests_gen.add_regression_tests(wt, _inv(), "diff", lambda p, r=reply: r)
        assert out["files"] == [] and len(out["notes"]) == 1, item["file"]
    assert (wt / "app.py").read_text().startswith("def total") and not (wt / "tests" / "test_bad.py").exists()
    assert (wt / "tests" / "test_util.py").read_text().startswith("from util")
    too_many = json.dumps({"tests": [{"file": f"tests/test_{i}.py", "content": "def test_a(): pass\n"} for i in range(5)]})
    assert len(tests_gen.add_regression_tests(wt, _inv(), "diff", lambda p: too_many)["files"]) == tests_gen.MAX_TEST_FILES


# ---- gap 6: read-only context, self-review ----


def test_context_files_reach_the_prompt_read_only_and_blocked_paths_do_not(repo, monkeypatch):
    llm = Router()
    _run(repo, monkeypatch, llm, context_files=["util.py", ".env", "../x.py"])
    first = llm.prompts[0]
    assert "READ-ONLY CONTEXT" in first and "def helper" in first and "NEVER edit them" in first
    assert ".env" not in first.split("READ-ONLY CONTEXT")[1].split("--- CODE")[0]


def test_a_passing_review_is_recorded(repo, monkeypatch):
    monkeypatch.setenv("CODING_SELF_REVIEW", "true")
    out = _run(repo, monkeypatch, Router(reviews=[PASS]))
    assert out["status"] == "completed" and out["review"] == "pass" and out["requires_approval"] is False


def test_a_failed_review_sends_the_problems_back_to_fix_once(repo, monkeypatch):
    monkeypatch.setenv("CODING_SELF_REVIEW", "true")
    llm = Router(reviews=[FAIL, PASS], retry_edit=_edit("return a + b", "return b + a"))
    out = _run(repo, monkeypatch, llm)
    assert out["review"] == "pass" and out["requires_approval"] is False
    retry_prompt = next(p for p in llm.prompts if "A REVIEWER REJECTED" in p)
    assert "reports.py" in retry_prompt


def test_a_review_that_keeps_failing_holds_the_change_for_a_human(repo, monkeypatch):
    monkeypatch.setenv("CODING_SELF_REVIEW", "true")
    out = _run(repo, monkeypatch, Router(reviews=[FAIL, FAIL], retry_edit=_edit("return a + b", "return b + a")))
    assert out["status"] == "completed" and out["review"] == "fail" and out["requires_approval"] is True
    assert any(r.startswith("reviewer:") and "reports.py" in r for r in out["remaining_risks"])


def test_an_unusable_review_is_a_risk_not_a_crash(repo, monkeypatch):
    monkeypatch.setenv("CODING_SELF_REVIEW", "true")
    out = _run(repo, monkeypatch, Router(reviews=["no idea"]))
    assert out["status"] == "completed" and out["requires_approval"] is True
    assert any("self-review did not run" in r for r in out["remaining_risks"])


@pytest.mark.parametrize("reply,expected", [
    (PASS, ("pass", [])),
    (FAIL, ("fail", ["only one call site was fixed: the off by one remains in reports.py"])),
    ("PASS", ("pass", [])),
    ("FAIL: too broad", ("fail", ["too broad"])),
    ('{"verdict": "PASS"}', ("pass", [])),
])
def test_review_verdict_parsing(reply, expected):
    v = review.parse_verdict(reply)
    assert (v["verdict"], v["problems"]) == expected
    assert review.parse_verdict("maybe") is None and review.parse_verdict('{"verdict": "unsure"}') is None


def test_a_fail_without_reasons_still_has_a_problem_text():
    assert review.review({}, "d", [], lambda p: '{"verdict": "fail", "problems": []}')["problems"]

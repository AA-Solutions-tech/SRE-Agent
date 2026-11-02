"""Second approval gate: commit, push and pull request only after a person approves. Real git, local remote."""
import subprocess

import pytest

from sre_agent.core import git_tools as g
from sre_agent.core.approvals import handle_decision
from sre_agent.core.db import create_item, get_item
from sre_agent.core.notify import approval_token
from sre_agent.sre import ship


def _sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def fix(tmp_path, cfg, monkeypatch):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _sh(remote, "git", "init", "--bare", "-b", "main")
    repo = tmp_path / "repo"
    repo.mkdir()
    _sh(repo, "git", "init", "-b", "main")
    _sh(repo, "git", "config", "user.email", "t@t")
    _sh(repo, "git", "config", "user.name", "t")
    (repo / "app.py").write_text("def total(a, b):\n    return a - b\n")
    _sh(repo, "git", "add", "app.py")
    _sh(repo, "git", "commit", "-m", "init")
    _sh(repo, "git", "remote", "add", "origin", str(remote))
    _sh(repo, "git", "push", "-u", "origin", "main")
    cfg(repo_dir=repo, worktrees_dir=tmp_path / "wt", base_branch="main", dry_run_push=True, test_cmd="",
        github_token="")
    monkeypatch.setenv("SHIP_MODE", "approve")  # these tests cover the approval card; auto mode is tested below
    wt, branch = g.create_worktree("INC1")
    (wt / "app.py").write_text("def total(a, b):\n    return a + b\n")
    sent = []
    monkeypatch.setattr("sre_agent.core.approvals.notify", lambda t, b, buttons=None, **kw: sent.append((t, b, buttons)))
    inv_id = create_item("investigation", {"signal_id": "s1", "service": "wave-node", "error": "total is wrong",
                                           "root_cause": "subtracts", "remediation": "add", "version": 1})
    result = {"status": "completed", "worktree": str(wt), "branch": branch, "changed_files": ["app.py"],
              "summary": "fix: 1 file", "tests": [{"command": "python syntax check app.py", "passed": True}],
              "remaining_risks": ["no TEST_CMD configured: tests were not run"]}
    return {"repo": repo, "remote": remote, "wt": wt, "branch": branch, "sent": sent, "inv": inv_id, "result": result}


def _decide(fix_id, action):
    return handle_decision(fix_id, ship.STAGE, action, approval_token(fix_id, ship.STAGE))


def _remote_branches(fix):
    return _sh(fix["repo"], "git", "ls-remote", "--heads", str(fix["remote"]))


def test_offer_shows_the_real_diff_and_the_checks(fix):
    fix_id = ship.offer(fix["result"], fix["inv"])
    title, body, buttons = fix["sent"][0]
    assert fix["branch"] in title and "a + b" in body and "app.py" in body
    assert "pass  python syntax check app.py" in body and "no TEST_CMD is set" in body
    assert "DRY_RUN_PUSH=true" in body and [b[0] for b in buttons] == ["Approve", "Reject"]
    assert get_item(fix_id)["status"] == "awaiting_ship_fix"


def test_nothing_is_offered_for_an_unfinished_fix(fix, monkeypatch):
    assert ship.offer({**fix["result"], "status": "failed"}, fix["inv"]) is None
    monkeypatch.setenv("SHIP_ENABLED", "false")
    assert ship.offer(fix["result"], fix["inv"]) is None and not fix["sent"]


def test_approve_in_dry_run_commits_but_does_not_push(fix):
    msg = _decide(ship.offer(fix["result"], fix["inv"]), "approve")
    assert "committed" in msg and "DRY_RUN_PUSH=true" in msg
    log = _sh(fix["wt"], "git", "log", "-1", "--format=%an|%B")
    assert log.startswith("AI Fix Bot|fix(wave-node): total is wrong") and "Root cause: subtracts" in log
    assert _remote_branches(fix).count("ai-fix") == 0
    assert _sh(fix["wt"], "git", "status", "--short") == ""


def test_approve_with_push_enabled_pushes_the_branch(fix, cfg):
    cfg(dry_run_push=False)
    msg = _decide(ship.offer(fix["result"], fix["inv"]), "approve")
    assert "pushed" in msg and fix["branch"] in _remote_branches(fix)
    assert "not a github.com remote" in msg  # a local path remote: no pull request, told how to proceed


def test_reject_commits_nothing_and_keeps_the_worktree(fix):
    msg = _decide(ship.offer(fix["result"], fix["inv"]), "reject")
    assert "discarded" in msg and fix["wt"].is_dir()
    assert _sh(fix["wt"], "git", "log", "-1", "--format=%s") == "init"
    assert "app.py" in _sh(fix["wt"], "git", "diff", "--cached", "--name-only")


def test_a_failed_push_keeps_the_commit_and_says_how_to_retry(fix, cfg):
    cfg(dry_run_push=False)
    _sh(fix["repo"], "git", "remote", "set-url", "origin", str(fix["remote"]) + "-missing")
    msg = _decide(ship.offer(fix["result"], fix["inv"]), "approve")
    assert "push failed" in msg and "git -C" in msg
    assert _sh(fix["wt"], "git", "log", "-1", "--format=%s").startswith("fix(wave-node)")


def test_second_approval_finds_nothing_to_commit(fix):
    fix_id = ship.offer(fix["result"], fix["inv"])
    ship.handle_ship(fix_id, "approve")
    assert "nothing to commit" in ship.handle_ship(fix_id, "approve")


def test_remote_urls_are_parsed():
    for url in ("git@github.com:AA-Solutions-tech/Wave.git", "https://github.com/AA-Solutions-tech/Wave.git",
                "https://user@github.com/AA-Solutions-tech/Wave", "ssh://git@github.com/AA-Solutions-tech/Wave.git"):
        assert ship.parse_remote(url) == ("AA-Solutions-tech", "Wave")
    assert ship.parse_remote("/tmp/remote.git") is None and ship.parse_remote("git@gitlab.com:a/b.git") is None


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


def test_pull_request_creation(fix, cfg, monkeypatch):
    d = {"worktree": str(fix["wt"]), "branch": fix["branch"], "tests": [], "remaining_risks": []}
    monkeypatch.setattr(ship, "_git", lambda *a, **k: "git@github.com:org/Wave.git")
    assert "compare/main..." in ship.open_pull_request(d, {}, d) and "no GITHUB_TOKEN" in ship.open_pull_request(d, {}, d)
    cfg(github_token="tok")
    seen = {}

    def post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        return _Resp(201, {"html_url": "https://github.com/org/Wave/pull/7"})

    monkeypatch.setattr(ship.httpx, "post", post)
    assert ship.open_pull_request(d, {"error": "e", "service": "s"}, d) == "https://github.com/org/Wave/pull/7"
    assert seen["url"].endswith("/repos/org/Wave/pulls") and seen["json"]["base"] == "main"
    assert seen["json"]["head"] == fix["branch"] and seen["headers"]["Authorization"] == "Bearer tok"
    monkeypatch.setattr(ship.httpx, "post", lambda *a, **k: _Resp(422, {"message": "A pull request already exists"}))
    assert "already exists" in ship.open_pull_request(d, {}, d)
    monkeypatch.setattr(ship.httpx, "post", lambda *a, **k: _Resp(500, {}))
    with pytest.raises(RuntimeError):
        ship.open_pull_request(d, {}, d)


# ---------------- auto mode: ship without asking ----------------
@pytest.fixture
def auto(fix, monkeypatch):
    monkeypatch.setenv("SHIP_MODE", "auto")
    mails = []
    monkeypatch.setattr(ship, "notify", lambda t, b, buttons=None, **kw: mails.append((t, b)))
    fix["mails"] = mails
    return fix


def test_auto_mode_commits_at_once_and_sends_a_summary_not_a_card(auto):
    ship.offer(auto["result"], auto["inv"])
    assert not auto["sent"]  # no approval card
    title, body = auto["mails"][0]
    assert title.startswith("fix shipped:") and "FIX SHIPPED" in body and "committed" in body
    assert _sh(auto["wt"], "git", "log", "-1", "--format=%s").startswith("fix(wave-node)")
    assert "DRY_RUN_PUSH=true" in body  # push stays off until the flag is flipped
    assert auto["branch"] not in _remote_branches(auto)


def test_auto_mode_pushes_and_opens_a_draft_pull_request_when_allowed(auto, cfg, monkeypatch):
    cfg(dry_run_push=False, github_token="tok")
    monkeypatch.setattr(ship, "parse_remote", lambda url: ("org", "Wave"))
    seen = {}

    def post(url, json, headers, timeout):
        seen.update(url=url, json=json)
        return _Resp(201, {"html_url": "https://github.com/org/Wave/pull/9"})

    monkeypatch.setattr(ship.httpx, "post", post)
    ship.offer(auto["result"], auto["inv"])
    assert auto["branch"] in _remote_branches(auto)
    assert seen["json"]["head"] == auto["branch"] and seen["json"]["base"] == "main"
    assert seen["json"]["draft"] is True  # no TEST_CMD: a draft, so nobody mistakes it for a tested change
    assert "pull/9" in auto["mails"][0][1]


def test_pull_request_is_ready_for_review_when_tests_ran(auto, cfg, monkeypatch):
    cfg(dry_run_push=False, github_token="tok", test_cmd="npm test")
    monkeypatch.setattr(ship, "parse_remote", lambda url: ("org", "Wave"))
    seen = {}
    monkeypatch.setattr(ship.httpx, "post", lambda url, json, headers, timeout: seen.update(json=json) or
                        _Resp(201, {"html_url": "u"}))
    ship.offer(auto["result"], auto["inv"])
    assert seen["json"]["draft"] is False


def test_a_high_risk_fix_is_held_for_approval_even_in_auto_mode(auto):
    ship.offer({**auto["result"], "high_risk": True}, auto["inv"])
    title, body, buttons = auto["sent"][0]
    assert "HELD FOR APPROVAL" in body and [b[0] for b in buttons] == ["Approve", "Reject"]
    assert not auto["mails"] and _sh(auto["wt"], "git", "log", "-1", "--format=%s") == "init"


def test_shipping_links_the_branch_and_pull_request_to_the_issue(auto, cfg, monkeypatch):
    from sre_agent.sre import issues

    sid = create_item("signal", {"service": "wave-node", "severity": "HIGH", "error": "total is wrong"})
    issue_id, _ = issues.record_occurrence({"service": "wave-node", "severity": "HIGH", "error": "total is wrong"}, sid)
    from sre_agent.core.db import update_item

    update_item(sid, issue_id=issue_id)
    update_item(auto["inv"], signal_id=sid)
    ship.offer(auto["result"], auto["inv"])
    d = get_item(issue_id)["data"]
    assert d["branch"] == auto["branch"] and d["commit"]
    from fastapi.testclient import TestClient

    from sre_agent import server

    assert TestClient(server.app).get(f"/issues/{issue_id}").json()["branch"] == auto["branch"]

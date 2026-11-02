"""Uses a real throwaway git repo (no network)."""
import subprocess

import pytest

from sre_agent.core import git_tools as g


def _sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, cfg):
    r = tmp_path / "repo"
    r.mkdir()
    _sh(r, "git", "init", "-b", "main")
    _sh(r, "git", "config", "user.email", "t@t")
    _sh(r, "git", "config", "user.name", "t")
    (r / "app.py").write_text("print('hi')\nprint('there')\n")
    (r / ".env").write_text("SECRET=1")
    _sh(r, "git", "add", "app.py")
    _sh(r, "git", "commit", "-m", "init")
    cfg(repo_dir=r, worktrees_dir=tmp_path / "wt", base_branch="main", dry_run_push=True, test_cmd="")
    return r


def tools_by_name(tools):
    return {t.name: t for t in tools}


def test_read_tools(repo):
    t = tools_by_name(g.make_read_tools(repo))
    assert "app.py" in t["list_dir"].invoke({"path": "."})
    assert ".git" not in t["list_dir"].invoke({"path": "."}).split("\n")
    assert t["read_file"].invoke({"path": "app.py", "start_line": 2, "end_line": 2}) == "2: print('there')"
    assert "app.py:1:" in t["git_grep"].invoke({"pattern": "hi"})
    assert t["git_grep"].invoke({"pattern": "zzzzz"}) == "no matches"
    assert "init" in t["git_log"].invoke({"path": "."})


def test_read_tools_refuse_secrets_and_escape(repo):
    read_file = tools_by_name(g.make_read_tools(repo))["read_file"]
    with pytest.raises(ValueError):
        read_file.invoke({"path": ".env"})
    with pytest.raises(ValueError):
        read_file.invoke({"path": "../x"})


def test_worktree_write_diff_commit_dry_run(repo):
    wt, branch = g.create_worktree("inc1")
    assert branch == "ai-fix/inc1" and wt.exists()
    write = tools_by_name(g.make_write_tools(wt))
    assert "wrote app.py" in write["write_file"].invoke({"path": "app.py", "content": "print('fixed')\n"})
    assert "+print('fixed')" in g.diff(wt)
    assert "DRY RUN" in g.commit_and_push(wt, branch, "fix: thing")
    assert (repo / "app.py").read_text().startswith("print('hi')")  # base checkout untouched
    g.remove_worktree(wt)
    assert not wt.exists()


def test_write_tool_blocked_paths(repo):
    wt, _ = g.create_worktree("inc2")
    write_file = tools_by_name(g.make_write_tools(wt))["write_file"]
    with pytest.raises(ValueError):
        write_file.invoke({"path": ".git/config", "content": "x"})
    with pytest.raises(ValueError):
        write_file.invoke({"path": "../../evil.py", "content": "x"})


def test_run_tests_tool(repo, cfg):
    wt, _ = g.create_worktree("inc3")
    run_tests = tools_by_name(g.make_write_tools(wt))["run_tests"]
    assert "no TEST_CMD" in run_tests.invoke({})
    cfg(test_cmd="python -c \"print('ok')\"")
    out = run_tests.invoke({})
    assert out.startswith("exit=0") and "ok" in out


def test_diff_empty_when_no_changes(repo):
    wt, _ = g.create_worktree("inc4")
    assert g.diff(wt) == ""


def test_duplicate_worktree_raises(repo):
    g.create_worktree("dup")
    with pytest.raises(RuntimeError):
        g.create_worktree("dup")

"""GitHub/Jira/chat tools with httpx mocked - verifies URLs, auth and output formatting."""
from sre_agent.core import tools as t


class Resp:
    def __init__(self, data=None, text=""):
        self._d, self.text = data, text

    def raise_for_status(self): pass
    def json(self): return self._d


def by_name(tl):
    return {x.name: x for x in tl}


def test_read_only_by_default():
    assert set(by_name(t.github_tools())) == {"gh_list_prs", "gh_get_pr_diff", "gh_list_issues"}
    assert "gh_comment" in by_name(t.github_tools(write=True))
    assert set(by_name(t.jira_tools())) == {"jira_search", "jira_get_issue"}
    assert {"jira_create_issue", "jira_comment"} <= set(by_name(t.jira_tools(write=True)))


def test_gh_list_prs(monkeypatch, cfg):
    cfg(github_token="tok")
    seen = {}

    def get(url, params=None, headers=None, timeout=None, **kw):
        seen.update(url=url, headers=headers)
        return Resp([{"number": 7, "state": "open", "title": "Fix", "user": {"login": "bob"}}])

    monkeypatch.setattr(t.httpx, "get", get)
    out = by_name(t.github_tools())["gh_list_prs"].invoke({"repo": "o/r"})
    assert out == "#7 [open] Fix (by bob)"
    assert seen["url"].endswith("/repos/o/r/pulls") and seen["headers"]["Authorization"] == "Bearer tok"


def test_gh_list_issues_skips_prs(monkeypatch):
    monkeypatch.setattr(t.httpx, "get", lambda *a, **k: Resp([
        {"number": 1, "title": "bug"}, {"number": 2, "title": "pr", "pull_request": {}}]))
    assert by_name(t.github_tools())["gh_list_issues"].invoke({"repo": "o/r"}) == "#1 bug"


def test_gh_pr_diff_truncated(monkeypatch):
    monkeypatch.setattr(t.httpx, "get", lambda *a, **k: Resp(text="d" * 20_000))
    assert len(by_name(t.github_tools())["gh_get_pr_diff"].invoke({"repo": "o/r", "number": 1})) == 15_000


def test_gh_comment(monkeypatch):
    seen = {}
    monkeypatch.setattr(t.httpx, "post", lambda url, json, headers, timeout: seen.update(url=url, json=json) or Resp({"html_url": "http://c"}))
    out = by_name(t.github_tools(write=True))["gh_comment"].invoke({"repo": "o/r", "number": 3, "body": "hi"})
    assert out == "http://c" and seen["url"].endswith("/repos/o/r/issues/3/comments") and seen["json"] == {"body": "hi"}


def test_jira_search_formats(monkeypatch, cfg):
    cfg(jira_base_url="http://j", jira_email="e", jira_api_token="k")
    seen = {}

    def get(url, params, auth, timeout):
        seen.update(url=url, params=params, auth=auth)
        return Resp({"issues": [{"key": "A-1", "fields": {"summary": "S", "status": {"name": "Done"}, "assignee": None}}]})

    monkeypatch.setattr(t.httpx, "get", get)
    out = by_name(t.jira_tools())["jira_search"].invoke({"jql": "project = A"})
    assert out == "A-1 [Done] S (unassigned)"
    assert seen["params"]["jql"] == "project = A" and seen["auth"] == ("e", "k")


def test_jira_search_empty(monkeypatch, cfg):
    cfg(jira_base_url="http://j")
    monkeypatch.setattr(t.httpx, "get", lambda *a, **k: Resp({"issues": []}))
    assert by_name(t.jira_tools())["jira_search"].invoke({"jql": "x"}) == "none"


def test_jira_create_issue_payload(monkeypatch, cfg):
    cfg(jira_base_url="http://j")
    seen = {}
    monkeypatch.setattr(t.httpx, "post", lambda url, json, auth, timeout: seen.update(json=json) or Resp({"key": "A-9"}))
    out = by_name(t.jira_tools(write=True))["jira_create_issue"].invoke({"project": "A", "summary": "Do it"})
    f = seen["json"]["fields"]
    assert out == "A-9" and f["project"] == {"key": "A"} and f["issuetype"] == {"name": "Task"}
    assert f["description"]["type"] == "doc"  # Atlassian Document Format


def test_chat_tool_calls_notify(monkeypatch):
    got = []
    monkeypatch.setattr(t, "notify", lambda title, msg: got.append((title, msg)))
    assert by_name(t.chat_tools())["post_to_chat"].invoke({"title": "T", "message": "M"}) == "sent"
    assert got == [("T", "M")]

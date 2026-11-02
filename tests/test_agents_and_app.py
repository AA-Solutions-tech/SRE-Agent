"""Sources, agent graphs (fake LLM), CLI parsing and the FastAPI approval endpoint."""
import json

import pytest
from fastapi.testclient import TestClient

from sre_agent import cli
from sre_agent.agents._template import agent as template
from sre_agent.agents.manager_report import agent as report
from sre_agent.agents.manager_report import sources
from sre_agent.core import db
from sre_agent.core.notify import approval_token


class FakeLLM:
    def __init__(self, text="LLM OUTPUT"):
        self.text, self.prompts = text, []

    def invoke(self, prompt):
        self.prompts.append(prompt)

        class M:
            content = self.text
        return M()


# ---------- run.py ----------
def test_cli_parse():
    assert cli.parse(["hello world", "--days", "7", "--repo-name", "x"]) == (["hello world"], {"days": 7, "repo_name": "x"})


def test_cli_list_and_unknown(capsys):
    assert cli.main(["run", "--list"]) == 0 and "hello" in capsys.readouterr().out
    assert cli.main(["run", "nope"]) == 1


# ---------- manager_report ----------
def test_standup_updates_reads_jsonl(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"user": "a", "text": "did x"}) + "\n\n")
    assert sources.standup_updates(str(p)) == [{"user": "a", "text": "did x"}]
    assert sources.standup_updates(str(tmp_path / "missing")) == []


def test_jira_activity_disabled_without_url(cfg):
    cfg(jira_base_url="")
    assert sources.jira_activity() == []


def test_github_activity_parses(monkeypatch, cfg):
    cfg(github_repos=("o/r",), github_token="t")

    class R:
        def __init__(self, d): self.d = d
        def json(self): return self.d

    def get(url, params=None, **kw):
        if url.endswith("/commits"):
            return R([{"author": None, "commit": {"author": {"name": "Ann"}, "message": "feat: x\n\nbody"}}])
        return R([{"title": "PR", "user": {"login": "bob"}, "state": "closed", "merged_at": "2999-01-01", "updated_at": "2999-01-01"}])

    monkeypatch.setattr(sources.httpx, "get", get)
    out = sources.github_activity(7)[0]
    assert out["commits"] == [{"author": "Ann", "message": "feat: x"}]
    assert out["pull_requests"][0]["merged"] is True


def test_manager_report_pipeline(monkeypatch):
    llm, sent = FakeLLM("REPORT"), []
    monkeypatch.setattr(report, "get_llm", lambda: llm)
    monkeypatch.setattr(report, "github_activity", lambda d: [{"repo": "o/r"}])
    monkeypatch.setattr(report, "jira_activity", lambda d: [{"key": "A-1"}])
    monkeypatch.setattr(report, "standup_updates", lambda: [])
    monkeypatch.setattr(report, "notify", lambda title, body: sent.append((title, body)))
    assert report.run(days=3) == "REPORT"
    assert "o/r" in llm.prompts[0] and "A-1" in llm.prompts[0]
    assert sent == [("Weekly engineering report", "REPORT")]


# ---------- template + approval over HTTP ----------
@pytest.fixture
def client():
    from sre_agent import server
    return TestClient(server.app)


def test_template_propose_then_approve_via_http(monkeypatch, client):
    sent = []
    monkeypatch.setattr("sre_agent.core.approvals.notify", lambda *a, **k: sent.append(("approval", a, k)))
    monkeypatch.setattr(template, "notify", lambda title, body: sent.append(("done", title, body)))
    state = template.propose({"input": "x", "analysis": "do the thing"})
    item = state["item_id"]
    assert db.get_item(item)["status"] == "awaiting_act"

    assert client.get(f"/approve/{item}/act/approve?token=bad").status_code == 403
    r = client.get(f"/approve/{item}/act/approve?token={approval_token(item, 'act')}")
    assert r.status_code == 200 and "executed" in r.text
    assert db.get_item(item)["status"] == "act_approved"
    assert sent[-1][0] == "done"
    # link is single-use
    assert client.get(f"/approve/{item}/act/approve?token={approval_token(item, 'act')}").status_code == 409


def test_http_reject_and_unknown(client):
    item = db.create_item("template", {"proposal": "p"}, status="awaiting_act")
    r = client.get(f"/approve/{item}/act/reject?token={approval_token(item, 'act')}")
    assert r.status_code == 200 and "Rejected" in r.text
    assert client.get(f"/approve/ghost/act/approve?token={approval_token('ghost', 'act')}").status_code == 404


def test_health_and_run_endpoint(client, monkeypatch):
    assert client.get("/health").json()["ok"] is True
    assert client.post("/run/nope", json={}).status_code == 404
    monkeypatch.setattr("sre_agent.agents.hello.agent.run", lambda question: f"echo {question}")
    assert client.post("/run/hello", json={"question": "hi"}).json() == {"result": "echo hi"}

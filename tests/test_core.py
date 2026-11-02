import pytest

from sre_agent.core import approvals, db
from sre_agent.core.git_tools import safe_path
from sre_agent.core.notify import approval_token


def test_safe_path_blocks_escape_and_secrets(tmp_path):
    (tmp_path / "a.py").write_text("x")
    assert safe_path(tmp_path, "a.py") == (tmp_path / "a.py").resolve()
    with pytest.raises(ValueError):
        safe_path(tmp_path, "../etc/passwd")
    with pytest.raises(ValueError):
        safe_path(tmp_path, ".env")


def test_approval_flow_is_signed_and_single_use():
    calls = []

    @approvals.on_approval("t")
    def h(item_id, action):
        calls.append(action)
        return "ok"

    item = db.create_item("test", {"a": 1})
    db.update_item(item, status="awaiting_t")
    with pytest.raises(PermissionError):
        approvals.handle_decision(item, "t", "approve", "bad")
    assert approvals.handle_decision(item, "t", "approve", approval_token(item, "t")) == "ok"
    with pytest.raises(ValueError):  # second click rejected
        approvals.handle_decision(item, "t", "approve", approval_token(item, "t"))
    assert calls == ["approve"]


def test_approval_rejects_bad_action_unknown_item_and_missing_handler():
    tok = approval_token("nope", "t2")
    with pytest.raises(ValueError):
        approvals.handle_decision("nope", "t2", "maybe", tok)
    with pytest.raises(LookupError):
        approvals.handle_decision("nope", "t2", "approve", tok)
    item = db.create_item("test", {}, status="awaiting_nohandler")
    with pytest.raises(LookupError):
        approvals.handle_decision(item, "nohandler", "approve", approval_token(item, "nohandler"))


def test_request_approval_sets_status_and_sends_buttons(monkeypatch):
    sent = {}
    monkeypatch.setattr(approvals, "notify", lambda title, body, buttons=None: sent.update(t=title, b=buttons))
    item = db.create_item("test", {})
    approvals.request_approval(item, "act", "Title", "Body")
    assert db.get_item(item)["status"] == "awaiting_act"
    assert [b[0] for b in sent["b"]] == ["Approve", "Reject"]
    assert "/approve/" + item + "/act/approve?token=" in sent["b"][0][1]


def test_agent_discovery():
    from sre_agent.agents import agent_names
    assert {"hello", "manager_report"} <= set(agent_names())
    assert "_template" not in agent_names()
    assert "_template" in agent_names(include_template=True)

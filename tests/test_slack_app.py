"""Slack channel: signature checks, modal on button click, submission feeds the revision service. Offline."""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from sre_agent import server
from sre_agent.core import notify as n
from sre_agent.core.db import get_item, list_items
from sre_agent.core.notify import approval_token
from sre_agent.sre import revision, slack_app
from test_revision import cards, plan  # noqa: F401  (shared fixtures)

client = TestClient(server.app)
SECRET = "s3cret"


@pytest.fixture(autouse=True)
def slack_env(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)


def _post(payload: dict, secret=SECRET, ts=None):
    raw = urlencode({"payload": json.dumps(payload)}).encode()
    ts = str(int(ts if ts is not None else time.time()))
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + raw, hashlib.sha256).hexdigest()
    return client.post("/slack/interactions", content=raw, headers={
        "content-type": "application/x-www-form-urlencoded", "x-slack-request-timestamp": ts, "x-slack-signature": sig})


def _value(plan):
    return f"request_changes|{plan['inv']}|1|{approval_token(plan['inv'], 'feedback-v1')}"


def test_signature_rules():
    raw, ts = b"payload=x", "1000"
    sig = "v0=" + hmac.new(b"k", b"v0:1000:" + raw, hashlib.sha256).hexdigest()
    assert slack_app.verify_signature(raw, ts, sig, "k", now=1100)
    assert not slack_app.verify_signature(raw, ts, sig, "other", now=1100)
    assert not slack_app.verify_signature(raw + b"!", ts, sig, "k", now=1100)
    assert not slack_app.verify_signature(raw, ts, sig, "k", now=1000 + 301)  # stale: replay protection
    assert not slack_app.verify_signature(raw, "abc", sig, "k") and not slack_app.verify_signature(raw, ts, "", "k")


def test_unsigned_or_unconfigured_requests_are_refused(monkeypatch):
    assert client.post("/slack/interactions", content=b"payload=%7B%7D").status_code == 401
    assert _post({"type": "block_actions"}, secret="wrong").status_code == 401
    assert _post({"type": "block_actions"}, ts=time.time() - 1000).status_code == 401
    monkeypatch.delenv("SLACK_SIGNING_SECRET")
    assert client.post("/slack/interactions", content=b"payload=%7B%7D").status_code == 404


def test_card_button_opens_the_modal(plan, cards, monkeypatch):
    opened = []
    monkeypatch.setattr(slack_app, "open_modal", lambda trigger, view: opened.append((trigger, view)))
    r = _post({"type": "block_actions", "user": {"id": "U1"}, "trigger_id": "T123",
               "actions": [{"action_id": "request_changes", "value": _value(plan)}]})
    assert r.status_code == 200
    trigger, view = opened[0]
    assert trigger == "T123" and view["callback_id"] == slack_app.CALLBACK
    assert view["private_metadata"].startswith(f"{plan['inv']}|1|")
    ids = [b.get("block_id") for b in view["blocks"] if b["type"] == "input"]
    assert ids == ["note", "deny_paths", "allow_paths", "extra_files", "extra_tests", "ops_only"]


def test_a_user_off_the_list_gets_no_modal(plan, cards, monkeypatch):
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U9,U8")
    told, opened = [], []
    monkeypatch.setattr(slack_app, "_tell", lambda payload, text: told.append(text))
    monkeypatch.setattr(slack_app, "open_modal", lambda *a: opened.append(a))
    _post({"type": "block_actions", "user": {"id": "U1"}, "trigger_id": "T",
           "actions": [{"action_id": "request_changes", "value": _value(plan)}]})
    assert not opened and "not on the list" in told[0]


def test_a_forged_button_value_is_refused(plan, cards, monkeypatch):
    told, opened = [], []
    monkeypatch.setattr(slack_app, "_tell", lambda payload, text: told.append(text))
    monkeypatch.setattr(slack_app, "open_modal", lambda *a: opened.append(a))
    _post({"type": "block_actions", "user": {"id": "U1"}, "trigger_id": "T",
           "actions": [{"action_id": "request_changes", "value": f"request_changes|{plan['inv']}|1|{'0' * 24}"}]})
    assert not opened and told and "invalid token" in told[0]


def _submission(plan, **values):
    state = {k: {"v": {"type": "plain_text_input", "value": v}} for k, v in values.items() if k != "ops_only"}
    if "ops_only" in values:
        state["ops_only"] = {"v": {"type": "checkboxes",
                                   "selected_options": [{"value": "on"}] if values["ops_only"] else []}}
    return {"type": "view_submission", "user": {"id": "U1"}, "view": {
        "callback_id": slack_app.CALLBACK, "state": {"values": state},
        "private_metadata": f"{plan['inv']}|1|{approval_token(plan['inv'], 'feedback-v1')}"}}


def test_submission_creates_version_two_and_closes_the_modal(plan, cards):
    r = _post(_submission(plan, note="please skip b.js", deny_paths="apps/node/src/routes/b.js", ops_only=False))
    assert r.status_code == 200 and r.json() == {"response_action": "clear"}
    v2 = [i for i in list_items(kind="investigation") if i["data"].get("version") == 2]
    assert len(v2) == 1 and v2[0]["data"]["constraints"]["deny_paths"] == ["apps/node/src/routes/b.js"]
    assert get_item(plan["inv"])["status"] == revision.SUPERSEDED


def test_bad_submission_shows_the_reason_inside_the_modal(plan, cards):
    r = _post(_submission(plan, note="", deny_paths=""))
    assert r.json()["response_action"] == "errors" and "note or set" in r.json()["errors"]["note"]
    r = _post(_submission(plan, note="x", deny_paths="../etc"))
    assert "not allowed" in r.json()["errors"]["note"]
    assert get_item(plan["inv"])["status"] == revision.AWAITING  # nothing was claimed


def test_card_gets_a_real_slack_button_only_when_the_app_is_configured(monkeypatch):
    btns = [("Request changes", "http://form", "", "request_changes|ID|1|TOK")]
    el = n._slack_blocks("t", "b", btns)[-1]["elements"][0]
    assert el["action_id"] == "request_changes" and el["value"] == "request_changes|ID|1|TOK" and "url" not in el
    monkeypatch.delenv("SLACK_BOT_TOKEN")
    assert n._slack_blocks("t", "b", btns)[-1]["elements"][0]["url"] == "http://form"

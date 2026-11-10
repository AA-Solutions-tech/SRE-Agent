"""Reviewer feedback: constraints, versions, links, form endpoints, escalation. Offline (no LLM, no repo)."""
import pytest
from fastapi.testclient import TestClient

from sre_agent import server
from sre_agent.core.approvals import handle_decision
from sre_agent.core.db import create_item, get_item, list_items
from sre_agent.core.notify import approval_token, feedback_url
from sre_agent.sre import issues, revision
from sre_agent.sre.incident import agent as incident
from sre_agent.sre.incident.rca import build_analysis_card

client = TestClient(server.app)

ANALYSIS = {
    "summary": "null subscription document", "impact": "500 on /account", "category": "code_bug",
    "fix_type": "code", "root_cause": "subscriptionDoc is destructured without a null check",
    "remediation": "add a guard", "affected_files": ["apps/node/src/routes/a.js", "apps/node/src/routes/b.js"],
    "code_refs": [{"path": "apps/node/src/routes/a.js", "line": 10}, {"path": "apps/node/src/routes/b.js", "line": 20}],
    "confidence": 0.8, "required_tests": ["unit test for the null case"], "evidence": ["log line"],
}


@pytest.fixture
def cards(monkeypatch):
    sent = []
    monkeypatch.setattr("sre_agent.core.approvals.notify", lambda t, b, buttons=None, **kw: sent.append((t, b, buttons)))
    monkeypatch.setattr(revision, "notify", lambda t, b, buttons=None: sent.append((t, b, buttons)))
    return sent


@pytest.fixture
def plan(cards):
    """A version-1 plan awaiting approval, linked to a signal and an issue."""
    signal = {"service": "wave-node", "severity": "HIGH", "error": "Failed to fetch", "evidence": ["e"],
              "ts": "2026-10-22 10:00:00:000", "fingerprint": "fp1"}
    sid = create_item("signal", signal)
    issue_id, _ = issues.record_occurrence(signal, sid)
    from sre_agent.core.db import update_item

    update_item(sid, issue_id=issue_id)
    inv_id = create_item("investigation", {"signal_id": sid, "service": "wave-node", "severity": "HIGH",
                                           "error": "Failed to fetch", "rca": "x", "version": 1, **ANALYSIS},
                         status=revision.AWAITING)
    issues.attach_investigation(issue_id, inv_id)
    return {"inv": inv_id, "signal": sid, "issue": issue_id}


def _token(item_id, version=1):
    return approval_token(item_id, f"feedback-v{version}")


# ---------------- pure rules ----------------
def test_paths_are_validated():
    assert revision.clean_path("./apps\\node\\x.js") == "apps/node/x.js"
    for bad in ("/etc/passwd", "C:\\Windows", "../x", "a/../../b", ".env", "node_modules/x", ".git/config"):
        with pytest.raises(ValueError):
            revision.clean_path(bad)
    c, errors = revision.parse_constraints({"deny_paths": "a.js\nb/", "extra_tests": "t1\n\nt2", "ops_only": "on"})
    assert c["deny_paths"] == ["a.js", "b"] and c["extra_tests"] == ["t1", "t2"] and c["force_fix_type"] == "ops"
    assert not errors and revision.parse_constraints({"allow_paths": "../x"})[1]


def test_constraints_are_enforced_in_code(tmp_path):
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    (repo / "apps/node/src/routes").mkdir(parents=True)
    (repo / "apps/node/src/routes/c.js").write_text("x")
    c = {"deny_paths": ["apps/node/src/routes/b.js"], "allow_paths": [], "extra_tests": ["case X"],
         "extra_files": ["apps/node/src/routes/c.js", "apps/node/src/routes/missing.js"], "force_fix_type": None}
    out, notes = revision.apply_constraints(ANALYSIS, c, repo)
    assert out["affected_files"] == ["apps/node/src/routes/a.js", "apps/node/src/routes/c.js"]
    assert "case X" in out["required_tests"] and any("missing.js" in n and "ignored" in n for n in notes)
    out, _ = revision.apply_constraints(ANALYSIS, {"allow_paths": ["apps/node/src/routes/a.js"], "deny_paths": []}, None)
    assert out["affected_files"] == ["apps/node/src/routes/a.js"]
    out, _ = revision.apply_constraints(ANALYSIS, {"deny_paths": ["apps/node"], "allow_paths": []}, None)
    assert out["affected_files"] == [] and out["confidence"] <= 0.3
    assert revision.apply_constraints(ANALYSIS, {"force_fix_type": "ops"}, None)[0]["fix_type"] == "ops"


def test_diff_lists_what_changed():
    new = {**ANALYSIS, "affected_files": ["apps/node/src/routes/a.js"], "fix_type": "ops", "confidence": 0.3}
    changes = revision.diff_versions(ANALYSIS, new)
    assert any("no longer edited" in c and "b.js" in c for c in changes) and any("fix type" in c for c in changes)
    assert any("confidence" in c for c in changes) and revision.diff_versions(ANALYSIS, ANALYSIS) == []


def test_card_shows_version_and_changes():
    _, body = build_analysis_card({"service": "s", "severity": "HIGH", "error": "e"}, ANALYSIS, 3, "sig",
                                  version=2, changes=["files now edited: c.js"], instruction="also fix c.js",
                                  revisions_left=2)
    assert "Plan version 2" in body and "WHAT CHANGED SINCE VERSION 1" in body and "also fix c.js" in body
    assert "Request changes" in body and "2 revision(s) left" in body


# ---------------- the revision itself ----------------
def test_revision_creates_version_two_and_kills_the_old_links(plan, cards):
    note, c = revision.begin(plan["inv"], 1, _token(plan["inv"]), "do not touch b.js",
                             {"deny_paths": "apps/node/src/routes/b.js"})
    assert get_item(plan["inv"])["status"] == revision.REVISING
    new_id = revision.revise(plan["inv"], note, c)
    v2 = get_item(new_id)
    assert v2["data"]["version"] == 2 and v2["data"]["previous_id"] == plan["inv"] and v2["status"] == revision.AWAITING
    assert v2["data"]["affected_files"] == ["apps/node/src/routes/a.js"]
    assert any("model could not re-analyse" in x for x in v2["data"]["changes"])
    assert get_item(plan["inv"])["status"] == revision.SUPERSEDED
    with pytest.raises(ValueError):  # the old Approve link is dead
        handle_decision(plan["inv"], incident.STAGE, "approve", approval_token(plan["inv"], incident.STAGE))
    assert issues.get_issue(plan["issue"])["data"]["investigation_id"] == new_id
    assert get_item(plan["issue"])["data"]["revisions"] == 1
    title, body, buttons = cards[-1]
    assert "Plan version 2" in body and "WHAT CHANGED SINCE VERSION 1" in body and "b.js" in body
    assert [b[0] for b in buttons] == ["Approve", "Reject", "I'll fix this", "Ignore", "Ignore similar", "Request changes"]
    assert buttons[-1][1] == feedback_url(new_id, 2)
    assert list_items(kind="plan_revision")[0]["data"]["new_investigation_id"] == new_id


def test_version_two_can_be_approved_and_carries_constraints_to_coding(plan, cards):
    note, c = revision.begin(plan["inv"], 1, _token(plan["inv"]), "x", {"deny_paths": "apps/node/src/routes/b.js"})
    new_id = revision.revise(plan["inv"], note, c)
    msg = handle_decision(new_id, incident.STAGE, "approve", approval_token(new_id, incident.STAGE))
    assert "fix" in msg and get_item(plan["issue"])["data"]["state"] == "fix_pending"
    assert get_item(new_id)["data"]["constraints"]["deny_paths"] == ["apps/node/src/routes/b.js"]


def test_refusals(plan, cards, monkeypatch):
    with pytest.raises(PermissionError):
        revision.begin(plan["inv"], 1, "bad", "x", {})
    with pytest.raises(ValueError, match="note or set"):
        revision.begin(plan["inv"], 1, _token(plan["inv"]), "  ", {})
    with pytest.raises(ValueError, match="not allowed"):
        revision.begin(plan["inv"], 1, _token(plan["inv"]), "x", {"deny_paths": "../secrets"})
    monkeypatch.setenv("REVISION_NOTE_MAX_CHARS", "5")
    with pytest.raises(ValueError, match="too long"):
        revision.begin(plan["inv"], 1, _token(plan["inv"]), "123456", {})
    monkeypatch.delenv("REVISION_NOTE_MAX_CHARS")
    revision.begin(plan["inv"], 1, _token(plan["inv"]), "ok", {})
    with pytest.raises(ValueError, match="already handled"):  # a second click while revising
        revision.begin(plan["inv"], 1, _token(plan["inv"]), "again", {})
    monkeypatch.setenv("REVISION_ENABLED", "false")
    with pytest.raises(LookupError):
        revision.begin(plan["inv"], 1, _token(plan["inv"]), "x", {})


def test_expired_link_and_wrong_version(plan, cards, monkeypatch):
    with pytest.raises(ValueError, match="newer version"):
        revision.begin(plan["inv"], 2, _token(plan["inv"], 2), "x", {})
    monkeypatch.setenv("REVISION_LINK_TTL_HOURS", "0")
    with pytest.raises(ValueError, match="expired"):
        revision.begin(plan["inv"], 1, _token(plan["inv"]), "x", {})


def test_revision_limit_escalates_to_a_person(plan, cards, monkeypatch):
    monkeypatch.setenv("REVISION_MAX", "1")
    note, c = revision.begin(plan["inv"], 1, _token(plan["inv"]), "first", {})
    v2 = revision.revise(plan["inv"], note, c)
    with pytest.raises(ValueError, match="limit"):
        revision.begin(v2, 2, _token(v2, 2), "second", {})
    assert any("revision limit reached" in t for t, _, _ in cards)
    assert get_item(v2)["status"] == revision.AWAITING  # still open for approve or reject


def test_failed_revision_reopens_the_old_version(plan, cards, monkeypatch):
    note, c = revision.begin(plan["inv"], 1, _token(plan["inv"]), "x", {})
    monkeypatch.setattr(revision, "revise", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert revision.run_revision(plan["inv"], note, c) is None
    assert get_item(plan["inv"])["status"] == revision.AWAITING
    assert any("revision failed" in t for t, _, _ in cards)


# ---------------- the form endpoints ----------------
def test_form_renders_and_rejects_a_bad_token(plan, cards):
    ok = client.get(f"/feedback/{plan['inv']}/1", params={"token": _token(plan["inv"])})
    assert ok.status_code == 200 and "Request changes to the plan" in ok.text and "wave-node" in ok.text
    assert client.get(f"/feedback/{plan['inv']}/1", params={"token": "nope"}).status_code == 403
    assert client.get("/feedback/unknown/1", params={"token": _token("unknown")}).status_code == 404


def test_post_creates_version_two_then_the_old_form_is_closed(plan, cards):
    r = client.post(f"/feedback/{plan['inv']}/1", data={"token": _token(plan["inv"]), "note": "skip b.js",
                                                        "deny_paths": "apps/node/src/routes/b.js"})
    assert r.status_code == 200 and "Thank you" in r.text
    versions = [i for i in list_items(kind="investigation") if i["data"].get("version") == 2]
    assert len(versions) == 1 and versions[0]["data"]["instruction"] == "skip b.js"
    again = client.get(f"/feedback/{plan['inv']}/1", params={"token": _token(plan["inv"])})
    assert again.status_code == 409 and "newer version" in again.text


def test_form_errors_are_escaped_and_keep_what_was_typed(plan, cards, monkeypatch):
    monkeypatch.setenv("REVISION_NOTE_MAX_CHARS", "5")
    evil = "<script>alert(1)</script>"
    r = client.post(f"/feedback/{plan['inv']}/1", data={"token": _token(plan["inv"]), "note": evil})
    assert r.status_code == 400 and "too long" in r.text
    assert "&lt;script&gt;" in r.text and "<script>alert" not in r.text
    assert get_item(plan["inv"])["status"] == revision.AWAITING  # a refused request claims nothing


def test_oversized_body_is_refused(plan):
    r = client.post(f"/feedback/{plan['inv']}/1", content=b"x" * 30_000,
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413

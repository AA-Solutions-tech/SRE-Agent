"""A coding run that fails is not a dead end; merged pull requests are followed and verified. Offline."""
from datetime import datetime, timedelta, timezone

import pytest

from sre_agent import agents
from sre_agent.core.approvals import handle_decision
from sre_agent.core.db import create_item, get_item, update_item
from sre_agent.core.notify import approval_token
from sre_agent.sre import auto, fixfail, issues, ship

NOW = datetime.now(timezone.utc)


@pytest.fixture
def cards(monkeypatch):
    sent = []
    monkeypatch.setattr("sre_agent.core.approvals.notify", lambda t, b, buttons=None, **kw: sent.append((t, b, buttons)))
    return sent


def _world(error="TypeError: boom"):
    """signal -> issue (fix approved) -> investigation, linked the way the real loop links them."""
    sig = create_item("signal", {"service": "api", "error": error, "severity": "HIGH"})
    issue_id, _ = issues.record_occurrence({"service": "api", "error": error, "severity": "HIGH", "evidence": ["x"]}, sig)
    update_item(sig, issue_id=issue_id)
    inv = create_item("investigation", {"signal_id": sig, "service": "api", "severity": "HIGH", "error": error,
                                        "rca": "r", "fix_type": "code"})
    issues.attach_investigation(issue_id, inv)
    issues.mark_fix_pending(issue_id)
    return sig, issue_id, inv


def _decide(item_id, action):
    return handle_decision(item_id, fixfail.STAGE, action, approval_token(item_id, fixfail.STAGE))


# ---- the failure card ----


def test_a_failed_run_reopens_the_issue_and_offers_a_retry(cards):
    sig, issue_id, inv = _world()
    fix_id = fixfail.report_failure(inv, {"status": "failed", "failure_reason": "tests failed: 2 errors"})
    d = get_item(issue_id)["data"]
    assert d["state"] == "open" and "tests failed" in d["fix_failed_reason"]
    assert "the automatic fix failed" in issues.describe(d)
    title, body, buttons = cards[0]
    assert title.startswith("fix failed: api HIGH TypeError: boom")
    assert [b[0] for b in buttons] == ["Retry the fix", "I'll fix this", "Ignore"]
    assert "tests failed: 2 errors" in body and "Nothing was committed or pushed" in body
    assert get_item(inv)["data"]["coding_attempts"] == 1 and get_item(fix_id)["data"]["retryable"] is True


def test_a_blocked_plan_is_not_retryable_and_says_what_to_change(cards):
    sig, issue_id, inv = _world()
    fixfail.report_failure(inv, {"status": "blocked", "failure_reason": "investigation.confidence 0.1 below minimum 0.5",
                                 "clarification_needed": ["investigation.confidence 0.1 below minimum 0.5",
                                                          "investigation.evidence is missing/empty"]})
    title, body, buttons = cards[0]
    assert [b[0] for b in buttons] == ["I'll fix this", "Ignore"]
    assert "blocked before it started" in body and "Retry is not offered" in body
    assert "investigation.evidence is missing/empty" in body


def test_retries_stop_at_the_limit(cards, monkeypatch):
    monkeypatch.setenv("CODING_MAX_RETRIES", "2")
    sig, issue_id, inv = _world()
    update_item(inv, coding_attempts=2)  # this is the third failure
    fixfail.report_failure(inv, {"status": "failed", "failure_reason": "x"})
    assert [b[0] for b in cards[0][2]] == ["I'll fix this", "Ignore"] and "retry limit (2)" in cards[0][1]


def test_retry_runs_the_coding_agent_again_and_marks_the_fix_pending(cards, monkeypatch):
    sig, issue_id, inv = _world()
    fix_id = fixfail.report_failure(inv, {"status": "failed", "failure_reason": "x"})
    started = []
    monkeypatch.setattr(auto, "enabled", lambda: True)
    monkeypatch.setattr(auto, "after_approval", lambda i, attempt=1: started.append((i, attempt)))
    assert "retrying the fix (attempt 2)" in _decide(fix_id, "approve")
    assert started == [(inv, 2)] and get_item(issue_id)["data"]["state"] == "fix_pending"


def test_retry_without_auto_mode_says_so_and_starts_nothing(cards, monkeypatch):
    sig, issue_id, inv = _world()
    fix_id = fixfail.report_failure(inv, {"status": "failed", "failure_reason": "x"})
    started = []
    monkeypatch.setattr(auto, "enabled", lambda: False)
    monkeypatch.setattr(auto, "after_approval", lambda *a, **k: started.append(a))
    assert "SRE_AUTO_RUN is off" in _decide(fix_id, "approve") and not started


def test_developer_and_ignore_from_the_failure_card(cards):
    for action, state in (("developer", "developer_fix"), ("ignore", "ignored")):
        sig, issue_id, inv = _world(f"Error {action}")
        fix_id = fixfail.report_failure(inv, {"status": "failed", "failure_reason": "x"})
        _decide(fix_id, action)
        assert get_item(issue_id)["data"]["state"] == state


def test_a_failure_card_link_works_once(cards):
    sig, issue_id, inv = _world()
    fix_id = fixfail.report_failure(inv, {"status": "failed", "failure_reason": "x"})
    _decide(fix_id, "ignore")
    with pytest.raises(ValueError):
        _decide(fix_id, "ignore")


# ---- the auto loop routes every non-success into it ----


class _SyncThread:
    def __init__(self, target=None, args=(), daemon=None, name=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def coding_stub(monkeypatch):
    calls = {"run": [], "reports": [], "offers": []}
    state = {"result": {"status": "failed", "failure_reason": "tests failed"}}

    class Coding:
        @staticmethod
        def run(incident_id="", signal=None, investigation=None):
            calls["run"].append(incident_id)
            if isinstance(state["result"], Exception):
                raise state["result"]
            return state["result"]

    monkeypatch.setenv("SRE_AUTO_RUN", "true")
    monkeypatch.setattr(auto.threading, "Thread", _SyncThread)
    monkeypatch.setattr(agents, "agent_names", lambda: ["coding"])
    monkeypatch.setattr(agents, "load_agent", lambda n: Coding)
    monkeypatch.setattr(fixfail, "report_failure", lambda inv, result=None, error="": calls["reports"].append((inv, result, error)))
    monkeypatch.setattr(ship, "offer", lambda out, inv: calls["offers"].append(inv) or "fx")
    return calls, state


def test_a_failed_run_goes_to_the_failure_flow_not_to_silence(coding_stub):
    calls, state = coding_stub
    sig, issue_id, inv = _world()
    auto.after_approval(inv)
    assert calls["run"] == [inv] and calls["reports"][0][:2] == (inv, state["result"]) and not calls["offers"]


def test_a_blocked_run_and_a_crash_do_too(coding_stub):
    calls, state = coding_stub
    sig, issue_id, inv = _world()
    state["result"] = {"status": "blocked", "failure_reason": "low confidence"}
    auto.after_approval(inv)
    state["result"] = RuntimeError("llm exploded")
    auto.after_approval(inv)
    assert len(calls["reports"]) == 2 and "crashed: RuntimeError: llm exploded" in calls["reports"][1][2]


def test_a_retry_uses_a_fresh_incident_id(coding_stub):
    calls, state = coding_stub
    sig, issue_id, inv = _world()
    auto.after_approval(inv, attempt=2)
    auto.after_approval(inv, attempt=3)
    assert calls["run"] == [f"{inv}-r1", f"{inv}-r2"]


def test_a_completed_run_is_offered_for_shipping_and_not_reported_as_failed(coding_stub):
    calls, state = coding_stub
    state["result"] = {"status": "completed", "worktree": "w", "branch": "b"}
    sig, issue_id, inv = _world()
    auto.after_approval(inv)
    assert calls["offers"] == [inv] and not calls["reports"]


def test_a_shipping_crash_is_reported_not_treated_as_a_failed_fix(coding_stub, monkeypatch):
    calls, state = coding_stub
    state["result"] = {"status": "completed", "worktree": "w", "branch": "b"}
    alerts = []
    monkeypatch.setattr(ship, "offer", lambda out, inv: (_ for _ in ()).throw(RuntimeError("git exploded")))
    monkeypatch.setattr("sre_agent.sre.health.alert", lambda key, title, detail, **kw: alerts.append(title) or True)
    sig, issue_id, inv = _world()
    auto.after_approval(inv)
    assert not calls["reports"] and alerts == ["A finished fix could not be handed over"]


# ---- pull request tracking ----


def _pr_issue(**extra):
    sig, issue_id, inv = _world()
    update_item(issue_id, pr_number=7, pr_repo="org/Wave", pr_url="https://github.com/org/Wave/pull/7", pr_state="open",
                **extra)
    return issue_id


@pytest.fixture
def mails(monkeypatch):
    out = []
    monkeypatch.setattr(ship, "notify", lambda t, b, buttons=None, **kw: out.append((t, b)))
    return out


def test_pr_url_is_split_into_repo_and_number_on_the_issue():
    sig, issue_id, inv = _world()
    ship._link_issue({"signal_id": sig, "branch": "ai-fix/x"}, "abc123", "https://github.com/org/Wave/pull/7")
    d = get_item(issue_id)["data"]
    assert (d["pr_number"], d["pr_repo"], d["pr_state"]) == (7, "org/Wave", "open")
    ship._link_issue({"signal_id": sig, "branch": "ai-fix/x"}, "abc123", "(origin is not a github.com remote)")
    assert get_item(issue_id)["data"]["pr_number"] is None


def test_a_merged_pull_request_starts_verification(mails, monkeypatch):
    issue_id = _pr_issue()
    monkeypatch.setattr(ship, "pr_status", lambda o, r, n: {"state": "closed", "merged": True,
                                                            "merged_at": (NOW - timedelta(minutes=3)).isoformat()})
    assert ship.poll_prs(NOW) == ["merged"]
    d = get_item(issue_id)["data"]
    assert d["pr_state"] == "merged" and d["merged_at"] and d["state"] == "fix_pending"
    assert mails[0][0].startswith("fix merged: api HIGH") and "stays away" in mails[0][1]
    assert ship.poll_prs(NOW) == []  # not polled again


def test_a_pull_request_closed_without_merging_reopens_the_issue(mails, monkeypatch):
    issue_id = _pr_issue()
    monkeypatch.setattr(ship, "pr_status", lambda o, r, n: {"state": "closed", "merged": False, "merged_at": None})
    assert ship.poll_prs(NOW) == ["closed"]
    d = get_item(issue_id)["data"]
    assert d["state"] == "open" and "closed without being merged" in d["fix_failed_reason"]
    assert mails[0][0].startswith("fix failed: api HIGH")


def test_a_pull_request_nobody_reviews_is_reminded_once_per_interval(mails, monkeypatch):
    issue_id = _pr_issue()
    update_item(issue_id, state_since=(NOW - timedelta(hours=25)).isoformat())
    monkeypatch.setattr(ship, "pr_status", lambda o, r, n: {"state": "open", "merged": False, "merged_at": None})
    assert ship.poll_prs(NOW) == ["reminded"] and "waiting for review" in mails[0][0]
    assert ship.poll_prs(NOW) == []


def test_unknown_pull_request_status_changes_nothing(mails, monkeypatch):
    issue_id = _pr_issue()
    monkeypatch.setattr(ship, "pr_status", lambda o, r, n: None)
    assert ship.poll_prs(NOW) == [] and get_item(issue_id)["data"]["state"] == "fix_pending" and not mails


def test_pr_status_needs_a_token(cfg):
    cfg(github_token="")
    assert ship.pr_status("o", "r", 1) is None


# ---- verification rules in the issue lifecycle ----


def test_an_unmerged_fix_is_not_called_fixed_just_because_the_error_went_quiet():
    issue_id = _pr_issue()
    update_item(issue_id, last_seen=(NOW - timedelta(hours=3)).isoformat())
    assert issues.sweep_all(NOW)["verified"] == [] and get_item(issue_id)["data"]["state"] == "fix_pending"


def test_a_merged_fix_must_stay_quiet_for_a_full_window_after_the_merge():
    issue_id = _pr_issue()
    update_item(issue_id, last_seen=(NOW - timedelta(hours=3)).isoformat(), pr_state="merged",
                merged_at=(NOW - timedelta(minutes=10)).isoformat())
    assert issues.sweep_all(NOW)["verified"] == []  # quiet, but only 10 min since the merge
    update_item(issue_id, merged_at=(NOW - timedelta(hours=2)).isoformat())
    res = issues.sweep_all(NOW)
    assert [(v["id"], v["was"]) for v in res["verified"]] == [(issue_id, "fix_pending")]
    assert get_item(issue_id)["data"]["state"] == "fixed"


def test_errors_right_after_a_merge_are_the_old_version_still_running():
    issue_id = _pr_issue()
    update_item(issue_id, merged_at=(NOW - timedelta(minutes=5)).isoformat(), pr_state="merged")
    _, action = issues.record_occurrence({"service": "api", "error": "TypeError: boom", "severity": "HIGH"}, "s2")
    assert action == "count" and get_item(issue_id)["data"]["state"] == "fix_pending"


def test_an_error_long_after_the_merge_means_the_fix_did_not_work():
    issue_id = _pr_issue()
    update_item(issue_id, merged_at=(NOW - timedelta(minutes=40)).isoformat(), pr_state="merged")
    _, action = issues.record_occurrence({"service": "api", "error": "TypeError: boom", "severity": "HIGH"}, "s2")
    d = get_item(issue_id)["data"]
    assert action == "investigate" and d["state"] == "open" and d["reopened_after_merge_min"] >= 39
    assert d["merged_at"] is None and d["pr_number"] is None and d["regressions"] == 1
    assert "the fix did not work" in issues.describe(d)

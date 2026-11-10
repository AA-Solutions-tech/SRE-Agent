"""Pilot gaps 1, 2, 3, 5, 6: the model call, the labelled fallback, grouping, severity, the ignore list. Offline."""
import json
import sys
import types

import pytest

from sre_agent.core import llm
from sre_agent.core.approvals import handle_decision
from sre_agent.core.db import get_item, list_items
from sre_agent.core.notify import approval_token
from sre_agent.sre import auto, ignorelist, issues, llm_check
from sre_agent.sre import severity as sev
from sre_agent.sre.incident import agent as incident
from sre_agent.sre.incident import rca as rca_mod
from sre_agent.sre.monitor import agent as monitor

TS = "[2026-10-09T11:00:01Z]"


@pytest.fixture
def cards(monkeypatch):
    out = []
    monkeypatch.setattr("sre_agent.core.approvals.notify", lambda t, b, buttons=None, **kw: out.append((t, b, buttons)))
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: None)
    return out


def _write(tmp_path, *lines, name="app.log"):
    p = tmp_path / name
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return p


# ---------------- gap 1: no temperature unless the operator asks for one ----------------


@pytest.fixture
def fake_provider(monkeypatch, cfg):
    seen = {}

    class Chat:
        def __init__(self, **kw):
            seen.update(kw)

    for module, cls in (("langchain_anthropic", "ChatAnthropic"), ("langchain_openai", "ChatOpenAI")):
        monkeypatch.setitem(sys.modules, module, types.SimpleNamespace(**{cls: Chat}))
    monkeypatch.delenv("LLM_TEMPERATURE", raising=False)
    return seen, cfg


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_no_temperature_is_sent_by_default_whatever_the_caller_asks(fake_provider, provider):
    seen, cfg = fake_provider
    cfg(llm_provider=provider, llm_model="m")
    llm.get_llm(temperature=0.0)  # every caller in the agent passes this
    assert "temperature" not in seen and seen["model"] == "m"
    seen.clear()
    llm.get_llm()
    assert "temperature" not in seen


def test_the_operator_can_still_set_a_temperature(fake_provider, monkeypatch):
    seen, cfg = fake_provider
    cfg(llm_provider="anthropic")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.3")
    llm.get_llm(temperature=0.0)
    assert seen["temperature"] == 0.3
    seen.clear()
    monkeypatch.setenv("LLM_TEMPERATURE", "not-a-number")
    llm.get_llm()
    assert "temperature" not in seen


def test_other_kwargs_still_reach_the_provider(fake_provider):
    seen, cfg = fake_provider
    cfg(llm_provider="anthropic")
    llm.get_llm(max_tokens=123)
    assert seen["max_tokens"] == 123


# ---------------- gap 2: a template must say it is a template ----------------


def _broken_llm(monkeypatch, why="BadRequestError: temperature is not supported for this model (HTTP 400)"):
    def boom(**k):
        raise RuntimeError(why)

    monkeypatch.setattr(incident, "get_llm", boom)


def test_a_card_built_without_the_model_starts_with_the_reason(tmp_path, cards, monkeypatch):
    alerts = []
    monkeypatch.setattr("sre_agent.sre.health.alert", lambda key, title, detail, **kw: alerts.append((key, title, detail)) or True)
    _broken_llm(monkeypatch)
    log = _write(tmp_path, f"{TS} [ERROR] [api] TypeError: boom in handler")
    sid = monitor.run(source=str(log), mode="once").split()[1].rstrip(":")
    incident.run(signal_id=sid)
    title, body, buttons = cards[0]
    assert body.startswith("*AI ANALYSIS UNAVAILABLE*") and "temperature is not supported" in body
    assert "template" in body and "not a diagnosis" in body
    inv = list_items(kind="investigation")[0]["data"]
    assert "temperature is not supported" in inv["ai_error"]
    assert [a[0] for a in alerts] == ["llm_failing"] and "temperature is not supported" in alerts[0][2]


def test_a_card_with_a_real_analysis_has_no_banner(tmp_path, cards, monkeypatch):
    class Fake:
        def invoke(self, prompt):
            class M:
                content = "Likely cause: the handler dereferences undefined.\nEvidence: log\nSuggested fix: guard it."
            return M()

    monkeypatch.setattr(incident, "get_llm", lambda **k: Fake())
    log = _write(tmp_path, f"{TS} [ERROR] [api] TypeError: boom in handler")
    incident.run(signal_id=monitor.run(source=str(log), mode="once").split()[1].rstrip(":"))
    assert "AI ANALYSIS UNAVAILABLE" not in cards[0][1] and "dereferences undefined" in cards[0][1]
    assert not list_items(kind="investigation")[0]["data"].get("ai_error")


def test_the_structured_card_shows_the_banner_too():
    signal = {"service": "api", "severity": "HIGH", "error": "x", "ts": "t", "evidence": ["e"]}
    analysis = {"root_cause": "r", "confidence": 0.3, "fix_type": "code", "affected_files": [], "ai_error": "TimeoutError: slow"}
    _, body = rca_mod.build_analysis_card(signal, analysis, 1, "sid")
    assert body.startswith("*AI ANALYSIS UNAVAILABLE*") and "TimeoutError: slow" in body
    _, clean = rca_mod.build_analysis_card(signal, {**analysis, "ai_error": None}, 1, "sid")
    assert "AI ANALYSIS UNAVAILABLE" not in clean


def test_the_selftest_reports_the_providers_reason(monkeypatch, cfg):
    class Bad:
        def invoke(self, prompt):
            raise RuntimeError("401 invalid x-api-key")

    monkeypatch.setattr("sre_agent.core.llm.get_llm", lambda **k: Bad())
    ok, why = llm_check.selftest()
    assert ok is False and "invalid x-api-key" in why
    alerts = []
    monkeypatch.setattr("sre_agent.sre.health.alert", lambda key, title, detail, **kw: alerts.append((key, detail)) or True)
    assert llm_check.check_and_alert() is False
    assert alerts[0][0] == "llm_selftest" and "invalid x-api-key" in alerts[0][1]


def test_the_selftest_passes_quietly_when_the_model_answers(monkeypatch):
    class Good:
        def invoke(self, prompt):
            return types.SimpleNamespace(content="OK")

    monkeypatch.setattr("sre_agent.core.llm.get_llm", lambda **k: Good())
    monkeypatch.setattr("sre_agent.sre.health.alert", lambda *a, **k: pytest.fail("no alert expected"))
    assert llm_check.check_and_alert() is True


def test_startup_selftest_is_skipped_without_a_key_or_when_switched_off(monkeypatch, cfg):
    cfg(llm_provider="anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm_check.start_background() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("LLM_SELFTEST_ON_START", "false")
    assert llm_check.start_background() is False
    monkeypatch.setenv("LLM_SELFTEST_ON_START", "true")
    monkeypatch.setattr(llm_check, "check_and_alert", lambda: None)
    assert llm_check.start_background() is True


def test_the_doctor_makes_a_live_call_and_fails_with_the_reason(monkeypatch):
    from sre_agent import doctor

    monkeypatch.setattr(llm_check, "configured", lambda: True)
    monkeypatch.setattr(llm_check, "selftest", lambda: (False, "BadRequestError: temperature"))
    status, name, detail = doctor.check_llm_live()
    assert (status, name) == ("FAIL", "LLM call") and "temperature" in detail
    monkeypatch.setattr(llm_check, "selftest", lambda: (True, "OK"))
    assert doctor.check_llm_live()[0] == "OK"
    monkeypatch.setattr(llm_check, "configured", lambda: False)
    assert "skipped" in doctor.check_llm_live()[2]


# ---------------- gap 3: one cause, one incident ----------------

SAME_CAUSE = [
    ("URL not found: favicon.ico", "URL not found: wp-login.php", "URL not found: xmlrpc.php"),
    ("Route /a/b not found", "Route /x/y/z not found"),
    ('CORS rejected origin "https://a.com"', 'CORS rejected origin "https://b.org"'),
    ("Code ab12 invalid", "Code zz99 invalid"),
    ("Failed for host evil1.example.com", "Failed for host good.example.org"),
    ("GET /wp-admin/setup.php returned 404", "GET /phpmyadmin/index.php returned 404"),
    ("Cannot find module 'express'", "Cannot find module 'lodash'"),
]


@pytest.mark.parametrize("group", SAME_CAUSE)
def test_variants_of_one_message_share_a_fingerprint(group):
    assert len({issues.fingerprint("api", m) for m in group}) == 1


@pytest.mark.parametrize("a,b", [("TypeError: x is not a function", "RangeError: invalid array length"),
                                 ("MongoNetworkTimeout after 3 retries", "ECONNREFUSED 127.0.0.1:5432"),
                                 ("Failed to send email", "Failed to parse json")])
def test_genuinely_different_errors_stay_different(a, b):
    assert issues.fingerprint("api", a) != issues.fingerprint("api", b)
    assert issues.fingerprint("api", a) != issues.fingerprint("web", a)


def test_strict_mode_keeps_files_and_names_apart(monkeypatch):
    monkeypatch.setenv("FINGERPRINT_STRICT", "true")
    assert issues.fingerprint("s", "URL not found: favicon.ico") != issues.fingerprint("s", "URL not found: wp-login.php")
    assert issues.fingerprint("s", "failed for 123") == issues.fingerprint("s", "failed for 456")  # numbers still go


def test_ten_probe_lines_become_one_incident(tmp_path, cards, monkeypatch):
    names = ["report.pdf", "invoice.csv", "export.zip", "photo.png", "data.json", "backup.sql", "notes.txt", "a1b2c3.xml", "page.html", "theme.css"]
    log = _write(tmp_path, *[f"{TS} [ERROR] [shop] Upload rejected for file: {n}" for n in names])
    out = monitor.run(source=str(log), mode="once")
    assert len(list_items(kind="issue")) == 1 and "10 evidence lines" in out


# ---------------- gap 5: severity from what the error is ----------------


def _sig(error, *evidence, **kw):
    return {"service": "api", "error": error, "evidence": list(evidence) or [error], **kw}


ACCESS_404 = json.dumps({"level": "error", "message": "Not Found", "method": "GET", "url": "/favicon.ico", "statusCode": 404})
ACCESS_500 = json.dumps({"level": "error", "message": "boom", "method": "GET", "url": "/pay", "statusCode": 500})


@pytest.mark.parametrize("signal,expected", [
    (_sig("Not Found", ACCESS_404), "LOW"),
    (_sig("URL not found: wp-login.php"), "LOW"),
    (_sig("GET /x 404", "GET /x 404"), "LOW"),
    (_sig("CORS rejected origin https://evil.example"), "LOW"),
    (_sig("boom", ACCESS_500), None),
    (_sig("Request failed with status code 401", category="credentials"), None),  # OUR call to someone else: not noise
    (_sig("429 too many requests", category="rate_limit"), "MEDIUM"),
    (_sig("Failed login for admin", category="security"), "MEDIUM"),
    (_sig("TypeError: x is undefined"), None),
])
def test_built_in_severity_rules(signal, expected):
    assert sev.apply(signal).get("severity") == expected


def test_critical_beats_everything_and_defaults_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("CRITICAL_SERVICES", "api")
    assert sev.apply(_sig("URL not found: wp-login.php"))["severity"] == "CRITICAL"
    monkeypatch.delenv("CRITICAL_SERVICES")
    monkeypatch.setenv("SEVERITY_DEFAULTS", "false")
    assert sev.apply(_sig("URL not found: wp-login.php")) == {}


def test_rules_file_first_match_wins_and_can_target_one_service(tmp_path, monkeypatch):
    f = tmp_path / "severity.json"
    f.write_text(json.dumps({"rules": [
        {"match": "payment", "severity": "critical"},
        {"match": "[unclosed", "severity": "LOW"},           # does not compile: skipped, the rest still work
        {"match": "x", "severity": "BOGUS"},                  # not a severity: skipped
        {"match": "timeout", "service": "api", "severity": "MEDIUM"},
        {"match": "timeout", "severity": "LOW"}]}))
    monkeypatch.setenv("SEVERITY_RULES_FILE", str(f))
    assert sev.apply(_sig("payment failed"))["severity"] == "CRITICAL"
    assert sev.apply(_sig("db timeout"))["severity"] == "MEDIUM"                 # the service rule comes first
    assert sev.apply({**_sig("db timeout"), "service": "web"})["severity"] == "LOW"  # other services fall through
    assert sev.apply(_sig("something else")) == {}


def test_a_missing_or_broken_rules_file_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SEVERITY_RULES_FILE", str(tmp_path / "absent.json"))
    assert sev.apply(_sig("TypeError: x")) == {}
    (tmp_path / "broken.json").write_text("{not json")
    monkeypatch.setenv("SEVERITY_RULES_FILE", str(tmp_path / "broken.json"))
    assert sev.apply(_sig("TypeError: x")) == {}


def test_a_service_can_have_its_own_default(monkeypatch):
    monkeypatch.setenv("SERVICE_SEVERITY", "api=LOW, other=HIGH")
    assert sev.apply(_sig("TypeError: x"))["severity"] == "LOW"
    assert sev.apply({**_sig("TypeError: x"), "service": "other"})["severity"] == "HIGH"


def test_a_low_error_is_counted_quietly_not_posted(tmp_path, cards, monkeypatch):
    announced = []
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: announced.append(a))
    log = _write(tmp_path, f"{TS} [ERROR] [shop] URL not found: wp-login.php")
    out = monitor.run(source=str(log), mode="once")
    assert out.startswith("no new signals") and not announced and not cards
    issue = list_items(kind="issue")[0]["data"]
    assert issue["state"] == "low" and issue["severity"] == "LOW"
    assert list_items(kind="signal")[0]["status"] == "counted"


def test_a_low_error_becomes_a_card_once_it_is_frequent(tmp_path, monkeypatch):
    monkeypatch.setenv("LOW_ESCALATE_COUNT", "3")
    results = []
    for i in range(3):
        _, action = issues.record_occurrence({"service": "s", "error": "URL not found", "severity": "LOW", "evidence": ["x"]}, f"s{i}")
        results.append(action)
    assert results == ["count", "count", "investigate"]
    assert list_items(kind="issue")[0]["data"]["state"] == "open"


def test_a_fixed_issue_that_returns_as_noise_stays_quiet():
    issue_id, _ = issues.record_occurrence({"service": "s", "error": "Real bug", "severity": "HIGH", "evidence": ["x"]}, "s1")
    issues.mark_fixed(issue_id)
    _, action = issues.record_occurrence({"service": "s", "error": "Real bug", "severity": "LOW", "evidence": ["x"]}, "s2")
    assert action == "count" and get_item(issue_id)["data"]["state"] == "low"


def test_the_digest_lists_new_low_issues_once(monkeypatch):
    sent = []
    monkeypatch.setattr(auto, "notify", lambda t, b, buttons=None: sent.append((t, b)))
    for i in range(4):
        issues.record_occurrence({"service": "shop", "error": "URL not found", "severity": "LOW", "evidence": ["x"]}, f"s{i}")
    assert auto.send_low_digest() is True
    title, body = sent[0]
    assert title.startswith("digest: 1 low-priority issue(s)") and "shop · URL not found: 4 new" in body
    assert auto.send_low_digest() is False and len(sent) == 1  # nothing new since


# ---------------- gap 6: an ignore list ----------------


@pytest.fixture
def ignore_file(tmp_path, monkeypatch):
    monkeypatch.setenv("IGNORE_FILE", str(tmp_path / "ignore.json"))
    monkeypatch.delenv("IGNORE_PATTERNS", raising=False)
    return tmp_path / "ignore.json"


def test_env_and_file_patterns_both_apply(ignore_file, monkeypatch):
    monkeypatch.setenv("IGNORE_PATTERNS", "wp-login\\.php\nfavicon;;robots\\.txt")
    ignore_file.write_text(json.dumps({"ignore": [{"pattern": "healthcheck", "service": "api"}, {"pattern": "[bad"}]}))
    hit = lambda msg, svc="api": ignorelist.matches({"message": msg, "raw": msg, "service": svc}) is not None  # noqa: E731
    assert hit("GET /WP-LOGIN.PHP") and hit("favicon missing") and hit("robots.txt 404") and hit("healthcheck failed")
    assert not hit("healthcheck failed", svc="web") and not hit("TypeError: boom")  # the file rule is for api only
    assert len(ignorelist.rules()) == 4  # the pattern that does not compile is skipped


def test_ignored_lines_are_counted_but_never_become_a_signal(tmp_path, ignore_file, cards, monkeypatch):
    monkeypatch.setenv("IGNORE_PATTERNS", "wp-login\\.php")
    announced = []
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: announced.append(a))
    log = _write(tmp_path, *[f"{TS} [ERROR] [shop] URL not found: wp-login.php?{i}" for i in range(5)])
    out = monitor.run(source=str(log), mode="once")
    assert out.startswith("no new signals") and "5 ignored" in out and not announced
    assert not list_items(kind="issue") and not list_items(kind="signal")
    row = ignorelist.digest_rows()
    assert row == [{"pattern": "wp-login\\.php", "new": 5, "count": 5}] and ignorelist.digest_rows() == []


def test_other_errors_in_the_same_poll_still_get_through(tmp_path, ignore_file, cards, monkeypatch):
    monkeypatch.setenv("IGNORE_PATTERNS", "wp-login")
    log = _write(tmp_path, f"{TS} [ERROR] [shop] URL not found: wp-login.php", f"{TS} [ERROR] [shop] TypeError: real bug")
    assert monitor.run(source=str(log), mode="once").startswith("signal ")
    assert [i["data"]["error"] for i in list_items(kind="issue")] == ["TypeError"]


def test_adding_rules_is_validated_and_deduplicated(ignore_file):
    assert ignorelist.add("favicon\\.ico", service="Shop", note="bot") is True
    assert ignorelist.add("favicon\\.ico", service="shop") is False
    for bad, why in ((".*.*.*", "every line"), ("ab", "too short"), ("[oops(", "compile"), (".+?.*|.....", "every line")):
        with pytest.raises(ValueError, match=why):
            ignorelist.add(bad)
    data = json.loads(ignore_file.read_text())
    assert data["ignore"][0]["service"] == "shop" and len(data["ignore"]) == 1
    assert ignorelist.matches({"message": "GET /favicon.ico", "raw": "", "service": "shop"})  # reloaded after the write


def test_a_generic_error_name_is_not_ignored_blindly():
    assert ignorelist.pattern_for({"error": "Error"}) == "" and ignorelist.pattern_for({"error": "Failed"}) == ""
    assert ignorelist.pattern_for({"error": "URL not found"}) == "URL\\ not\\ found"


def test_ignore_similar_on_a_card_creates_a_standing_rule(tmp_path, ignore_file, cards, monkeypatch):
    log = _write(tmp_path, f"{TS} [ERROR] [shop] Payment widget failed: card 4242")
    sid = monitor.run(source=str(log), mode="once").split()[1].rstrip(":")
    incident.run(signal_id=sid)
    inv = list_items(kind="investigation")[0]["id"]
    assert [b[0] for b in cards[0][2]][-1] == "Ignore similar"
    msg = handle_decision(inv, incident.STAGE, "ignore_similar", approval_token(inv, incident.STAGE))
    assert "never posted" in msg and json.loads(ignore_file.read_text())["ignore"][0]["service"] == "shop"
    assert list_items(kind="issue")[0]["data"]["state"] == "ignored"
    again = _write(tmp_path, f"{TS} [ERROR] [shop] Payment widget failed: card 9999", name="again.log")
    assert "1 ignored" in monitor.run(source=str(again), mode="once")


def test_the_digest_also_reports_what_was_ignored(ignore_file, monkeypatch):
    sent = []
    monkeypatch.setattr(auto, "notify", lambda t, b, buttons=None: sent.append((t, b)))
    monkeypatch.setenv("IGNORE_PATTERNS", "wp-login")
    ev = {"message": "wp-login", "raw": "wp-login", "service": "s"}
    ignorelist.record([(ev, ignorelist.rules()[0])] * 3)
    assert auto.send_low_digest() is True
    assert "3 ignored line(s)" in sent[0][0] and "IGNORED BY YOUR RULES" in sent[0][1] and "wp-login" in sent[0][1]

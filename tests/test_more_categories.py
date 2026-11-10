"""Database, deploy/dependency, TLS, queue and security errors: classified, diagnosed, no code-fix card. Offline."""
import pytest

from sre_agent.sre import classify
from sre_agent.sre.incident import agent as incident
from sre_agent.sre.monitor import agent as monitor

TS = "[2026-10-09T11:00:01Z]"


def _sig(line):
    return {"service": "app", "error": line, "evidence": [line]}


@pytest.mark.parametrize("line,category", [
    ("Error: certificate has expired", "tls"),
    ("SSL: CERTIFICATE_VERIFY_FAILED unable to get local issuer", "tls"),
    ("UNABLE_TO_VERIFY_LEAF_SIGNATURE calling partner api", "tls"),
    ("Failed login for admin from 203.0.113.9", "security"),
    ("Too many failed login attempts for user bob", "security"),
    ("Possible SQL injection attempt blocked", "security"),
    ("deadlock detected while updating timesheets", "database"),
    ("FATAL: remaining connection slots are reserved", "database"),
    ('relation "timesheets" does not exist', "database"),
    ("MongoServerSelectionError: connection timed out", "database"),
    ("Error: Cannot find module 'express'", "deploy"),
    ("ModuleNotFoundError: No module named 'requests'", "deploy"),
    ("EACCES: permission denied, open '/var/log/app.log'", "deploy"),
    ("Queue is full, dropping job", "queue"),
    ("worker crashed while processing email job", "queue"),
])
def test_new_categories_are_recognised(line, category):
    assert classify.classify(_sig(line))["category"] == category


@pytest.mark.parametrize("line", [
    "MongoNetworkTimeout after 3 retries",                       # left to the LLM path on purpose
    "TypeError: Cannot read properties of undefined (reading 'x')",
    "Request failed with status code 500",                        # the app's own 500: a code question
    "ValueError: timed out waiting for lock in our own code path",
    "User bob logged in",
])
def test_ambiguous_errors_stay_on_the_code_path(line):
    assert classify.classify(_sig(line)) is None


def test_our_own_rejected_credentials_are_not_called_an_attack():
    assert classify.classify(_sig("SMTP authentication failed: 535 5.7.8"))["category"] == "credentials"
    assert classify.classify(_sig("Failed login for admin"))["category"] == "security"


def test_missing_module_name_is_extracted():
    assert classify.classify(_sig("Error: Cannot find module 'express'"))["details"]["variable"] == "express"


@pytest.mark.parametrize("line,needle", [
    ("Error: certificate has expired calling billing.example.com", "TLS certificate"),
    ("Failed login for admin from 203.0.113.9", "203.0.113.9"),
    ("deadlock detected while updating timesheets", "Database"),
    ("Error: Cannot find module 'express'", "express"),
    ("Queue is full, dropping job", "Background processing"),
])
def test_each_gets_a_notice_card_without_approve(tmp_path, monkeypatch, line, needle):
    cards = []
    monkeypatch.setattr("sre_agent.core.approvals.notify", lambda t, b, buttons=None: cards.append((t, b, buttons)))
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: None)
    log = tmp_path / "app.log"
    log.write_text(f"{TS} [ERROR] [app] {line}\n")
    out = monitor.run(source=str(log), mode="once")
    incident.run(signal_id=out.split()[1].rstrip(":"))
    title, body, buttons = cards[0]
    assert title.startswith("action needed: app ") and needle in body
    assert [b[0] for b in buttons] == ["I'll fix this", "Ignore", "Ignore similar"]

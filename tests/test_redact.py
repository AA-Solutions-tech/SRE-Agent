"""Secrets must not reach cards, chat, email or LLM prompts. Offline."""
import pytest

from sre_agent.core import notify as notify_mod
from sre_agent.core.redact import redact
from sre_agent.sre.incident import diagnose


@pytest.mark.parametrize("text,secret", [
    ("SMTP_PASSWORD=hunter2 failed", "hunter2"),
    ('{"user": "a", "password": "hunter2"}', "hunter2"),
    ("Authorization: Bearer abcdef123456789xyz", "abcdef123456789xyz"),
    ("GET https://x.io/a?api_key=SECRET123&b=1", "SECRET123"),
    ("connect postgres://app:pw123secret@db:5432/x", "pw123secret"),
    ("using key sk-abcdefghijklmnopqrstuvwx", "abcdefghijklmnopqrstuvwx"),
    ("gh token ghp_abcdefghijklmnopqrstuvwxyz0123", "ghp_abcdefghijklmnopqrstuvwxyz0123"),
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop", "eyJhbGciOiJIUzI1NiJ9"),
    ("aws AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END RSA PRIVATE KEY-----", "MIIBOgIBAAJBAK"),
])
def test_secrets_are_masked(text, secret):
    out = redact(text)
    assert secret not in out and "***" in out or "removed" in out


@pytest.mark.parametrize("text", [
    "Missing required env var STRIPE_SECRET_KEY",
    "dummy@example.com 550 5.1.1 user unknown",
    "connect ECONNREFUSED 127.0.0.1:5432",
    "http://localhost:8000/approve/abc123/incident_fix/approve?token=0123456789abcdef",
    "http://localhost:8000/feedback/abc123/1?token=0123456789abcdef",
])
def test_ordinary_text_and_our_own_links_are_left_alone(text):
    assert redact(text) == text


def test_redact_is_idempotent_and_never_raises():
    once = redact("password=hunter2")
    assert redact(once) == once
    assert redact(None) == "None" and redact(123) == "123"


def test_notify_redacts_title_and_body_but_not_button_links(monkeypatch, cfg):
    sent = []
    monkeypatch.setattr(notify_mod, "send_slack", lambda text, blocks=None: sent.append((text, blocks)))
    cfg(notify_channels=("slack",))
    link = "http://localhost:8000/approve/abc/incident_fix/approve?token=0123456789abcdef"
    notify_mod.notify("t password=hunter2", "body api_key=SUPERSECRET1", buttons=[("Go", link, "")])
    text, blocks = sent[0]
    assert "hunter2" not in text and "SUPERSECRET1" not in str(blocks)
    assert link in str(blocks)


def test_card_evidence_is_redacted():
    signal = {"service": "api", "severity": "HIGH", "error": "x", "ts": "t",
              "evidence": ["2026-10-09 [ERROR] login failed password=hunter2 for user a"]}
    analysis = {"summary": "s", "impact": "i", "category": "credentials", "confidence": 0.8, "root_cause": "r",
                "remediation": "m", "next_steps": [], "checks": []}
    _, body = diagnose.build_notice_card(signal, analysis, 1, "sid")
    assert "hunter2" not in body and "password=***" in body

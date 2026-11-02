"""Shared fixtures. Settings is a frozen dataclass, so we override fields via object.__setattr__."""
import os
import tempfile

import pytest

# Must run before any core/sre import: keep test events out of the real data/logs/sre.log.
os.environ["SRE_LOG_PATH"] = os.path.join(tempfile.gettempdir(), "sre-tests", "sre.log")
os.environ["SRE_TRACE"] = "false"

from sre_agent.core.config import settings


@pytest.fixture
def cfg():
    """cfg(name=value, ...) temporarily overrides settings fields; restored after the test."""
    saved = {}

    def _set(**kw):
        for k, v in kw.items():
            saved.setdefault(k, getattr(settings, k))
            object.__setattr__(settings, k, v)

    yield _set
    for k, v in saved.items():
        object.__setattr__(settings, k, v)


@pytest.fixture(autouse=True)
def _isolated_db(cfg, tmp_path):
    cfg(db_path=str(tmp_path / "test.db"), repo_dir=tmp_path / "no-repo",  # tests never read a real repo
        api_key="")  # API open unless a test sets a key


@pytest.fixture(autouse=True)
def _no_real_notifications(cfg, monkeypatch):
    """Tests must never message real people: no webhooks/SMTP, and any stray SMTP use fails loudly."""
    import smtplib

    cfg(slack_webhook="", gchat_webhook="", smtp_user="", email_to=())

    def _blocked(*a, **k):
        raise AssertionError("a test tried to open a real SMTP connection")

    monkeypatch.setattr(smtplib, "SMTP", _blocked)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _blocked)


@pytest.fixture(autouse=True)
def _offline_llm(monkeypatch):
    """Real keys in .env must never make tests call an LLM; agents then use their offline fallbacks."""
    for key in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)

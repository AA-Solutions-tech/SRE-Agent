"""Shared fixtures. Settings is a frozen dataclass, so we override fields via object.__setattr__."""
import os
import tempfile
import threading

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


@pytest.fixture(autouse=True)
def _finish_background_threads():
    """A test that starts the coding thread must not leave it writing into the NEXT test's database."""
    yield
    for t in threading.enumerate():
        if t.name == "sre-auto-coding" and t is not threading.current_thread():
            t.join(10)


@pytest.fixture(autouse=True)
def _fresh_anomaly_counters():
    """The spike detector keeps its counts in memory; one test's burst must not alert in another test."""
    from sre_agent.sre import anomaly

    anomaly.reset()
    yield
    anomaly.reset()


@pytest.fixture(autouse=True)
def _legacy_approval_links(monkeypatch):
    """Many older tests mint the original "<id>:<stage>" token; tests of the new link rules switch this off."""
    monkeypatch.setenv("APPROVAL_ACCEPT_LEGACY", "true")


@pytest.fixture(autouse=True)
def _coding_extras_off(monkeypatch):
    """Tests of the base coding flow stay deterministic; the regression-test and self-review steps have their own
    tests (tests/test_coding_gaps.py) that switch them on."""
    monkeypatch.setenv("CODING_SELF_REVIEW", "false")
    monkeypatch.setenv("CODING_WRITE_TESTS", "false")

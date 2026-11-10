"""Rule-based error classification and runtime checks. Offline."""
import socket

import pytest

from sre_agent.sre import classify, runtime_checks


def _sig(error, *evidence):
    return {"service": "app", "error": error, "evidence": list(evidence) or [error]}


@pytest.mark.parametrize("line,category", [
    ("Error: Missing required env var STRIPE_SECRET_KEY", "config"),
    ("SMTP_PASSWORD is not set", "config"),
    ("KeyError: 'DATABASE_URL'", "config"),
    ("No API key provided", "config"),
    ("Request failed with status code 401", "credentials"),
    ("invalid_grant: token has expired", "credentials"),
    ("535 5.7.8 Username and Password not accepted", "credentials"),
    ("Error 429 Too Many Requests", "rate_limit"),
    ("OpenAI rate limit reached for gpt", "rate_limit"),
    ("550 5.1.1 <dummy@example.com> user unknown", "data"),
    ("connect ECONNREFUSED 127.0.0.1:5432", "server"),
    ("ENOSPC: no space left on device", "server"),
    ("Error fetching report metrics: fetch failed", "external"),
    ("getaddrinfo ENOTFOUND api.partner.com", "external"),
])
def test_known_operational_errors_are_classified(line, category):
    assert classify.classify(_sig(line))["category"] == category


@pytest.mark.parametrize("line", [
    "TypeError: Cannot read properties of undefined (reading 'name')",
    "MongoNetworkTimeout after 3 retries",
    "ValueError: invalid literal for int()",
    "Processed 401 items",
])
def test_code_bugs_and_plain_text_are_left_to_the_llm_path(line):
    assert classify.classify(_sig(line)) is None
    assert classify.tag(_sig(line)) == {}


def test_config_names_the_variable_and_hosts_and_emails_are_extracted():
    found = classify.classify(_sig("Missing required env var STRIPE_SECRET_KEY"))
    assert found["details"]["variable"] == "STRIPE_SECRET_KEY"
    d = classify.classify(_sig("connect ECONNREFUSED 127.0.0.1:5432"))["details"]
    assert d["hosts"] == [["127.0.0.1", 5432]]
    d = classify.classify(_sig("550 user unknown", "send failed to dummy@example.com: 550 user unknown"))["details"]
    assert d["emails"] == ["dummy@example.com"]


def test_config_wins_over_credentials_and_preset_is_kept():
    assert classify.classify(_sig("API_KEY is missing, request unauthorized"))["category"] == "config"
    assert classify.classify({"category": "job_missed", "details": {"job": "j"}})["category"] == "job_missed"


# ---- runtime checks ----


def _env(tmp_path, monkeypatch, text):
    f = tmp_path / "app.env"
    f.write_text(text)
    monkeypatch.setenv("APP_ENV_FILES", str(f))
    return f


def test_env_status_reports_states_but_never_values(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, "REAL_KEY=sk-supersecret\nEMPTY_KEY=\nPLACE_KEY=change-me\nexport QUOTED='v'\n")
    results = {n: runtime_checks.env_status(n) for n in ("REAL_KEY", "EMPTY_KEY", "PLACE_KEY", "NOPE_KEY")}
    assert [results[n]["sources"][0]["state"] for n in results] == ["set", "empty", "placeholder", "missing"]
    assert "sk-supersecret" not in str(results)


def test_env_status_suggests_similar_names_and_handles_missing_file(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, "STRIPE_SECRET_KEY=x\n")
    assert runtime_checks.env_status("STRIPE_SECRET_KY")["similar"] == ["STRIPE_SECRET_KEY"]
    monkeypatch.setenv("APP_ENV_FILES", str(tmp_path / "gone.env"))
    assert runtime_checks.env_status("X_KEY")["sources"][0]["state"] == "unreadable (FileNotFoundError)"
    monkeypatch.delenv("APP_ENV_FILES")
    assert runtime_checks.env_status("X_KEY")["checked"] is False


def test_tcp_check_only_probes_private_hosts():
    assert runtime_checks.host_allowed("127.0.0.1") and runtime_checks.host_allowed("10.1.2.3")
    assert not runtime_checks.host_allowed("8.8.8.8") and not runtime_checks.host_allowed("evil.example.com")
    skipped = runtime_checks.tcp_check("8.8.8.8", 53)
    assert skipped["reachable"] is None and "skipped" in skipped["error"]


def test_tcp_check_reports_reachable_and_refused():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert runtime_checks.tcp_check("127.0.0.1", port)["reachable"] is True
    assert runtime_checks.tcp_check("127.0.0.1", port, timeout=1)["reachable"] is False


def test_extra_allowed_hosts(monkeypatch):
    monkeypatch.setenv("RUNTIME_CHECK_HOSTS", "db.internal.example")
    assert runtime_checks.host_allowed("db.internal.example")

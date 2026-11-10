"""Docker and journald log sources, multi-source config, paging/escalation delivery. Offline."""
import json

import pytest

from sre_agent.core import paging
from sre_agent.sre.monitor import log_source
from sre_agent.sre.monitor.command_logs import DockerReader, JournaldReader

FUTURE = "2999-01-01T00:00:00.000000000Z"
FUTURE2 = "2999-01-01T00:00:01.000000000Z"


def test_docker_reader_baselines_then_returns_only_new_lines_once():
    outputs = [f"{FUTURE} [ERROR] boom 1\n{FUTURE2} second line\nno-timestamp continuation\n"]
    calls = []
    r = DockerReader("web", runner=lambda args: calls.append(args) or outputs[0])
    assert r.read_new() == "" and not calls  # first call only sets the baseline
    assert r.read_new() == "[ERROR] boom 1\nsecond line\n"
    assert calls[0][:3] == ["docker", "logs", "--timestamps"] and calls[0][-1] == "web"
    assert calls[0][calls[0].index("--since") + 1] > "2000"
    assert r.read_new() == ""  # same lines come back from --since (inclusive): not processed twice
    outputs[0] += f"{FUTURE2} another line at the same instant\n"
    assert r.read_new() == "another line at the same instant\n"


def test_journald_reader_uses_json_entries_and_dedupes():
    def entry(us, msg, cursor):
        return json.dumps({"__REALTIME_TIMESTAMP": str(us), "__CURSOR": cursor, "MESSAGE": msg})

    out = [entry(999_000_000, "old", "c0"), entry(1_000_000_500, "first", "c1"), entry(1_000_000_900, "second", "c2"),
           "not json", json.dumps({"__REALTIME_TIMESTAMP": "1000000950", "MESSAGE": ["bytes"]})]
    calls = []
    r = JournaldReader("myapp.service", runner=lambda args: calls.append(args) or "\n".join(out), clock=lambda: 1000.0)
    assert r.read_new() == "first\nsecond\n"
    assert calls[0][:3] == ["journalctl", "-u", "myapp.service"] and "@1000" in calls[0]
    assert r.read_new() == ""
    out.append(entry(1_000_001_000, "third", "c3"))
    assert r.read_new() == "third\n"


@pytest.mark.parametrize("name", ["", "-rf", "a b", "x;rm -rf /", "$(whoami)", "a/b"])
def test_bad_names_are_refused(name):
    with pytest.raises(ValueError):
        DockerReader(name)
    with pytest.raises(ValueError):
        JournaldReader(name)


def test_get_source_understands_the_new_prefixes_and_still_defaults_to_files(tmp_path):
    assert isinstance(log_source.get_source("docker:web"), DockerReader)
    assert isinstance(log_source.get_source("journald:myapp.service"), JournaldReader)
    from sre_agent.sre.monitor.local_file import LocalFileReader

    assert isinstance(log_source.get_source(str(tmp_path / "a.log")), LocalFileReader)


def test_monitor_reads_a_docker_source_end_to_end(tmp_path, monkeypatch):
    from sre_agent.sre.monitor import agent as monitor

    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: None)
    reader = DockerReader("web", runner=lambda args: f"{FUTURE} 2026-10-09 13:18:04:184 [error] Error fetching x: boom\n")
    monkeypatch.setattr("sre_agent.sre.monitor.agent.get_source", lambda name: reader)
    reader.read_new()  # baseline
    assert monitor.run(source="docker:web", mode="once").startswith("signal ")


def test_auto_starts_one_watcher_per_source(monkeypatch):
    from sre_agent.sre import auto

    started = []
    monkeypatch.setenv("SRE_AUTO_RUN", "true")
    monkeypatch.setattr(auto, "_started", False)
    monkeypatch.setattr(auto, "_watch_loop", lambda s: None)
    monkeypatch.setattr(auto, "_issue_loop", lambda: None)
    monkeypatch.setattr("sre_agent.sre.health.loop", lambda: None)
    real = auto.threading.Thread
    monkeypatch.setattr(auto.threading, "Thread",
                        lambda target=None, args=(), **kw: started.append((target.__name__ if hasattr(target, "__name__") else target, args)) or real(target=lambda: None))
    assert auto.start("/var/log/a.log, docker:web ,journald:app.service") is True
    assert [a for t, a in started if a] == [("/var/log/a.log",), ("docker:web",), ("journald:app.service",)]


# ---- paging / escalation ----


def test_page_and_escalate_do_nothing_when_unconfigured(monkeypatch):
    for k in ("PAGE_WEBHOOK_URL", "ESCALATION_SLACK_WEBHOOK_URL", "ESCALATION_EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    assert paging.page("t", "b") is False and paging.escalate("t", "b") is False


def test_page_posts_a_redacted_payload_and_never_raises(monkeypatch):
    posts = []
    monkeypatch.setenv("PAGE_WEBHOOK_URL", "https://pager.example/hook")
    monkeypatch.setattr(paging, "_post", lambda url, payload: posts.append((url, payload)))
    assert paging.page("db down", "password=hunter2 refused", "CRITICAL") is True
    url, payload = posts[0]
    assert url == "https://pager.example/hook" and "hunter2" not in json.dumps(payload) and payload["severity"] == "CRITICAL"
    monkeypatch.setattr(paging, "_post", lambda *a: (_ for _ in ()).throw(RuntimeError("pager down")))
    assert paging.page("t", "b") is False


def test_escalate_uses_slack_and_email(monkeypatch):
    posts, mails = [], []
    monkeypatch.setenv("ESCALATION_SLACK_WEBHOOK_URL", "https://hooks.example/x")
    monkeypatch.setenv("ESCALATION_EMAIL_TO", "boss@example.com, cto@example.com")
    monkeypatch.setattr(paging, "_post", lambda url, payload: posts.append(payload))
    monkeypatch.setattr("sre_agent.core.notify.send_email", lambda subject, body, headers=None, to=None: mails.append(to))
    assert paging.escalate("ESCALATION: x", "waiting 9h") is True
    assert posts and mails == [["boss@example.com", "cto@example.com"]]

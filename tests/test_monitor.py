"""Monitor agent tests (offline: no LLM, no network, isolated SQLite DB)."""
import pytest

from sre_agent.sre.monitor import agent as monitor_agent
from sre_agent.sre.monitor import parse
from sre_agent.sre.monitor.local_file import LocalFileReader
from sre_agent.sre.monitor.log_source import LogSource, get_source
from sre_agent.core.db import list_items


@pytest.fixture
def notified(monkeypatch):
    """Capture notify() calls instead of sending them."""
    calls = []
    monkeypatch.setattr(
        monitor_agent, "notify", lambda t, b, buttons=None: calls.append((t, b))
    )
    return calls


def _write(path, text):
    path.write_text(text)
    return str(path)


def test_info_only_creates_no_signal(tmp_path, notified):
    p = _write(
        tmp_path / "app.log",
        "[2026-10-09T11:00:01Z] [INFO] [payment-api] all good\n"
        "[2026-10-09T11:00:02Z] [DEBUG] [payment-api] stats {}\n",
    )
    out = monitor_agent.run(source=p, mode="once")
    assert "no new signals" in out
    assert list_items(kind="signal") == []
    assert notified == []


def test_error_creates_signal_with_contract_fields(tmp_path, notified):
    p = _write(
        tmp_path / "app.log",
        "old error from before the agent started\n"
        "[2026-10-09T11:00:01Z] [INFO] [payment-api] request ok\n"
        "[2026-10-09T11:00:02Z] [ERROR] [payment-api] MongoNetworkTimeout after 3 retries\n"
        "[2026-10-09T11:00:03Z] [ERROR] [payment-api] checkout failed for order #8812\n",
    )
    # First run processes the whole file (exactly-once from zero)...
    out = monitor_agent.run(source=p, mode="once")
    items = list_items(kind="signal")
    assert len(items) == 2  # two DISTINCT errors -> two signals, never one blob
    by_error = {i["data"]["error"]: i["data"] for i in items}
    signal = by_error["MongoNetworkTimeout"]
    assert signal["service"] == "payment-api"
    assert signal["severity"] == "HIGH"
    assert len(signal["evidence"]) == 1
    assert signal["ts"] == "2026-10-09T11:00:02Z"
    assert signal["source"] == p
    assert signal["fingerprint"] and signal["fingerprint"] != by_error["checkout"]["fingerprint"]
    assert "old error from before" not in "\n".join(signal["evidence"])
    assert signal["quarantine"] == ["old error from before the agent started"]
    assert len(notified) == 2 and all("payment-api" in n[0] for n in notified)
    assert out.startswith("signal ") and "(+1 more" in out

    # ...appended lines fire a second, separate signal on the next run.
    with open(p, "a") as f:
        f.write(
            "[2026-10-09T11:00:04Z] [WARN] [payment-api] retry budget 80% consumed\n"
        )
    out = monitor_agent.run(source=p, mode="once")
    items = list_items(kind="signal")
    assert len(items) == 3
    second = items[0]["data"]  # newest-first
    assert second["severity"] == "MEDIUM"
    assert len(second["evidence"]) == 1
    assert "signal" in out


def test_second_identical_run_is_quiet(tmp_path, notified):
    p = _write(
        tmp_path / "app.log",
        "[2026-10-09T11:00:01Z] [ERROR] [api] boom\n",
    )
    monitor_agent.run(source=p, mode="once")  # fires
    assert len(list_items(kind="signal")) == 1
    out = monitor_agent.run(source=p, mode="once")  # nothing new
    assert "no new signals" in out
    assert len(list_items(kind="signal")) == 1


def test_rotation_restarts_from_beginning(tmp_path, notified):
    p = _write(tmp_path / "app.log", "x" * 500 + "\n")
    out = monitor_agent.run(source=p, mode="once")
    assert "no new signals" in out  # garbage quarantines, never pages
    tmp_path.joinpath("app.log").write_text(
        "[2026-10-09T11:00:01Z] [ERROR] [api] fresh after rotation\n"
    )
    out = monitor_agent.run(source=p, mode="once")
    assert "signal" in out
    assert list_items(kind="signal")[0]["data"]["error"] == "fresh"


def test_malformed_line_quarantined_not_paged(tmp_path, notified):
    p = _write(tmp_path / "app.log", "not a log line at all\n")
    out = monitor_agent.run(source=p, mode="once")
    assert "no new signals" in out
    assert list_items(kind="signal") == []
    assert notified == []
    with open(p, "a") as f:
        f.write("still not a log line\n")
    out = monitor_agent.run(source=p, mode="once")
    assert "no new signals" in out
    assert list_items(kind="signal") == []


def test_quarantine_attached_as_context(tmp_path, notified):
    # Garbage and a real error in the same unseen batch: the error pages,
    # the garbage rides along as context (and is never processed twice).
    p = _write(
        tmp_path / "app.log",
        "garbage!!!\n"
        "[2026-10-09T11:00:01Z] [ERROR] [api] real failure\n",
    )
    monitor_agent.run(source=p, mode="once")
    signal = list_items(kind="signal")[0]["data"]
    assert signal["quarantine"] == ["garbage!!!"]
    assert len(signal["evidence"]) == 1


def test_missing_file_raises_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="monitor log not found"):
        monitor_agent.run(source=str(tmp_path / "nope.log"), mode="once")


def test_get_source_factory():
    assert isinstance(get_source("local_file"), LocalFileReader)
    assert isinstance(get_source("/tmp/x.log"), LocalFileReader)
    assert isinstance(get_source("local_file"), LogSource)
    from sre_agent.core.ssh_logs import RemoteLogReader

    assert isinstance(get_source("ssh"), RemoteLogReader)


def test_monitor_discovered_by_runner():
    from sre_agent.agents import agent_names, load_agent

    assert "monitor" in agent_names()
    assert load_agent("monitor").run is monitor_agent.run


def test_classify_unit():
    parts = parse.classify(
        [
            "[t] [ERROR] [s] e",
            "[t] [WARN] [s] w",
            "[t] [INFO] [s] i",
            "[t] [DEBUG] [s] d",
            "garbage",
            "",
        ]
    )
    assert len(parts["signal"]) == 2
    assert len(parts["audit"]) == 2
    assert len(parts["quarantine"]) == 1


def test_wave_node_line_with_four_digit_fraction_parses():
    from sre_agent.sre.monitor.parse import parse_line

    for frac in ("139", "1815", "4"):
        ev = parse_line(f"2026-10-22 14:18:15:{frac} [error] Failed to fetch account details: boom")
        assert ev["parsed"] and ev["level"] == "ERROR" and ev["service"]


def test_json_lines_log_parses_levels_and_service():
    import json

    from sre_agent.sre.monitor.parse import classify, parse_line

    rec = {"timestamp": "2026-10-22T20:31:29.825Z", "level": "error", "category": "ErrorHandler",
           "message": "Request error: ENOENT: no such file", "service": "psa-api", "requestId": "abc"}
    ev = parse_line(json.dumps(rec))
    assert ev["parsed"] and ev["level"] == "ERROR" and ev["service"] == "psa-api"
    assert ev["ts"] == rec["timestamp"] and ev["message"].startswith("Request error")
    assert parse_line(json.dumps({**rec, "level": "fatal"}))["level"] == "ERROR"
    assert parse_line(json.dumps({**rec, "level": "warning"}))["level"] == "WARN"
    parts = classify([json.dumps(rec), json.dumps({**rec, "level": "info"}), "{not json", "{}"])
    assert (len(parts["signal"]), len(parts["audit"]), len(parts["quarantine"])) == (1, 1, 2)


def test_json_access_warn_below_500_is_audit_not_signal():
    import json

    from sre_agent.sre.monitor.parse import classify

    base = {"timestamp": "t", "level": "warn", "kind": "access", "service": "psa-api",
            "message": "POST /api/invoices/1/payments 400 28ms"}
    parts = classify([json.dumps({**base, "statusCode": 400}), json.dumps({**base, "statusCode": 403}),
                      json.dumps({**base, "statusCode": 502}), json.dumps({**base, "level": "error", "statusCode": 500})])
    assert len(parts["audit"]) == 2
    assert len(parts["signal"]) == 2


def test_local_reader_detects_rotation_by_rename(tmp_path):
    import os

    from sre_agent.sre.monitor.local_file import LocalFileReader

    log = tmp_path / "app.log"
    log.write_text("old line one\nold line two\n")
    r = LocalFileReader(str(log))
    assert "old line one" in r.read_new()
    os.rename(log, tmp_path / "app.log.1")
    log.write_text("brand new file, longer than everything the old file held so far\n")  # bigger, same path
    assert r.read_new().startswith("brand new file")


def test_local_reader_reads_a_big_backlog_over_several_polls(tmp_path):
    from sre_agent.sre.monitor.local_file import LocalFileReader

    log = tmp_path / "big.log"
    log.write_bytes(b"x" * 50 + b"\n")  # bytes: no Windows newline translation
    r = LocalFileReader(str(log))
    got = r.read_new(max_bytes=20) + r.read_new(max_bytes=20) + r.read_new(max_bytes=20)
    assert got == "x" * 50 + "\n"


def test_expand_path_follows_wildcards_to_the_newest_real_file(tmp_path):
    import os

    from sre_agent.sre.monitor.agent import expand_path, has_dynamic_parts

    old = tmp_path / "errors-2026-10-22.jsonl"
    new = tmp_path / "errors-2026-10-23.jsonl"
    gz = tmp_path / "errors-2026-10-24.jsonl.gz"
    for f, t in ((old, 1000), (new, 2000), (gz, 3000)):
        f.write_text("x")
        os.utime(f, (t, t))
    assert has_dynamic_parts(str(tmp_path / "errors-*.jsonl")) and not has_dynamic_parts("plain.log")
    assert expand_path(str(tmp_path / "errors-*")) == os.path.realpath(new)  # .gz skipped
    assert expand_path(str(tmp_path / "nothing-*.log")).endswith("nothing-*.log")  # no match: pattern kept

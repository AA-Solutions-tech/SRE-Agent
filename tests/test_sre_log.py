"""sre_log tests: JSON shape, redaction, levels via stdlib logging + caplog.

No tmp log files, no logger resets: caplog attaches to the shared `sre`
logger, which is exactly how production code consumes it.
"""
import json
import logging

import pytest

from sre_agent.core.sre_log import JsonFormatter, RedactFilter, _redact, get_logger


@pytest.fixture
def sre_caplog(caplog):
    logger = logging.getLogger("sre")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="sre")
    yield caplog
    logger.removeHandler(caplog.handler)


def test_formatter_shape():
    record = logging.LogRecord("sre.monitor", logging.INFO, __file__, 1,
                               "signal.filed", None, None)
    record.signal_id = "abc"
    line = json.loads(JsonFormatter().format(record))
    assert (line["logger"], line["level"], line["event"]) == ("sre.monitor", "INFO", "signal.filed")
    assert line["details"]["signal_id"] == "abc" and "ts" in line


def test_redact_masks_secret_keys():
    out = _redact({"CLOUDFLARE_API_TOKEN": "cf-secret-value-123", "password": "hunter2pw",
                   "headers": {"Authorization": "Bearer abcdef-key"},
                   "url": "https://h/approve/i/s/a?token=deadbeef999",
                   "error": "plain message stays"})
    raw = json.dumps(out)
    for secret in ("cf-secret-value-123", "hunter2pw", "Bearer abcdef-key", "deadbeef999"):
        assert secret not in raw
    assert "plain message stays" in raw
    assert out["url"].endswith("token=***")


def test_redact_filter_masks_message():
    record = logging.LogRecord("sre.x", logging.INFO, __file__, 1,
                               "see https://h/?token=abc123 ok", None, None)
    assert RedactFilter().filter(record) is True
    assert "abc123" not in record.msg and "token=***" in record.msg


def test_levels_info_warning_error(sre_caplog):
    log = get_logger("monitor")
    log.info("signal.filed", extra={"signal_id": "a"})
    log.warning("route.fallback", extra={"reason": "r"})
    log.error("delegate.failed", extra={"target": "t"})
    by_level = {r.levelname: r.getMessage() for r in sre_caplog.records}
    assert by_level == {"INFO": "signal.filed", "WARNING": "route.fallback", "ERROR": "delegate.failed"}


def test_monitor_run_logs_filed_line(tmp_path, monkeypatch, sre_caplog):
    monkeypatch.setattr("sre_agent.sre.monitor.agent.notify", lambda *a, **k: None)
    from sre_agent.sre.monitor import agent as monitor_agent

    log = tmp_path / "app.log"
    log.write_text("[2026-10-09T11:00:01Z] [ERROR] [payment-api] boom\n")
    monitor_agent.run(source=str(log), mode="once")
    filed = [r for r in sre_caplog.records
             if r.name == "sre.monitor" and r.getMessage() == "signal.filed"]
    assert len(filed) == 1
    assert filed[0].service == "payment-api"


def test_get_logger_namespaced():
    assert get_logger("incident").name == "sre.incident"

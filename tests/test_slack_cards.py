"""Slack card layout: kinds are told apart, bodies split per heading, unknown titles stay plain. Offline."""
from sre_agent.core import notify as n
from sre_agent.core import slack_cards as sc
from sre_agent.sre.incident.rca import build_analysis_card, build_card, pretty_line


def _texts(blocks):
    out = []
    for b in blocks:
        if b["type"] == "header":
            out.append(b["text"]["text"])
        elif b["type"] == "section":
            out.append(b["text"]["text"])
        elif b["type"] == "context":
            out.append(b["elements"][0]["text"])
    return out


def test_each_kind_gets_its_own_header():
    heads = {}
    for title in ("signal detected: api HIGH", "investigate: api HIGH boom [trace abc]", "fix ready: api boom"):
        blocks = sc.layout(title, "*SUMMARY*\nx")[0]
        heads[title] = blocks[0]["text"]["text"]
    assert len(set(heads.values())) == 3 and heads["signal detected: api HIGH"].startswith("🚨")


def test_unknown_title_keeps_plain_layout():
    assert sc.layout("Weekly engineering report", "body") is None
    blocks = n._slack_blocks("Weekly engineering report", "body", None)
    assert [b["type"] for b in blocks] == ["header", "section"]


def test_headline_shows_service_severity_trace_and_error():
    before, _ = sc.layout("investigate: psa-api HIGH [jira] KEY bad [trace 0e78b926f7]", "*SUMMARY*\nx")
    text = "\n".join(_texts(before))
    assert "*psa-api*" in text and "🟠 HIGH" in text and "trace `0e78b926f7`" in text
    assert "*[jira] KEY bad*" in text


def test_body_is_split_per_heading_and_on_approve_moves_under_buttons():
    body = "*SUMMARY*\ns\n\n*ROOT CAUSE*  (confidence 80%)\nr\n\n*ON APPROVE*\n• Approve: x"
    blocks = n._slack_blocks("investigate: api HIGH boom", body, [("Approve", "http://u", "primary")])
    kinds = [b["type"] for b in blocks]
    assert kinds.index("actions") < kinds.index("context", kinds.index("actions"))
    assert blocks[-1]["type"] == "context" and "Approve: x" in blocks[-1]["elements"][0]["text"]
    texts = "\n".join(_texts(blocks[: kinds.index("actions")]))
    assert "📝 *Summary*" in texts and "🧬 *Root cause*  _(confidence 80%)_" in texts and "ON APPROVE" not in texts


def test_supervisor_note_is_one_compact_block():
    blocks = n._slack_blocks("sre supervisor: api HIGH", "Signal routed", None)
    assert len(blocks) == 1 and "Routing" in blocks[0]["text"]["text"]


def test_sections_stay_inside_slack_limits():
    blocks = n._slack_blocks("investigate: api HIGH boom", "*SUMMARY*\n" + "x" * 9000, None)
    assert all(len(t) <= 3000 for t in _texts(blocks)) and len(blocks) <= 50


def test_pretty_line_unwraps_json_logs():
    line = '{"timestamp":"2026-11-03T09:33:44Z","level":"error","kind":"app","message":"KEY must be 32 bytes"}'
    assert pretty_line(line) == "2026-11-03T09:33:44Z error KEY must be 32 bytes"
    assert pretty_line("plain text") == "plain text"


def test_real_cards_render_without_raw_json():
    signal = {"service": "psa-api", "severity": "HIGH", "error": "boom", "ts": "t",
              "evidence": ['{"level":"error","message":"boom"}']}
    title, body = build_card(signal, "Likely cause: x", 1)
    text = "\n".join(_texts(n._slack_blocks(title, body, None)))
    assert '{"level"' not in text and "Log evidence" in text
    analysis = {"root_cause": "r", "summary": "s", "fix_type": "ops", "confidence": 0.5}
    title, body = build_analysis_card(signal, analysis, 1, "sid")
    assert "Root cause" in "\n".join(_texts(n._slack_blocks(title, body, None)))

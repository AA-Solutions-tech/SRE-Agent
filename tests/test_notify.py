from urllib.parse import parse_qs, urlparse

from sre_agent.core import notify as n


def test_token_is_deterministic_and_secret_dependent(cfg):
    cfg(approval_secret="s1")
    a = n.approval_token("id1", "act")
    assert a == n.approval_token("id1", "act") and len(a) == 24
    assert a != n.approval_token("id2", "act")
    cfg(approval_secret="s2")
    assert a != n.approval_token("id1", "act")


def test_approval_url_shape(cfg):
    cfg(app_base_url="http://x:8000", approval_secret="s")
    u = urlparse(n.approval_url("id1", "act", "approve"))
    q = parse_qs(u.query)
    assert u.path == "/approve/id1/act/approve"
    assert q["token"] == [n.approval_token("id1", "act", "approve", int(q["exp"][0]))]


def test_slack_blocks_buttons_and_truncation():
    blocks = n._slack_blocks("T" * 500, "B" * 5000, [("Go", "http://u", "primary"), ("No", "http://v", "")])
    assert len(blocks[0]["text"]["text"]) == 150 and len(blocks[1]["text"]["text"]) == 2800
    els = blocks[2]["elements"]
    assert els[0]["style"] == "primary" and "style" not in els[1]


def test_slack_blocks_without_buttons():
    assert len(n._slack_blocks("t", "b", None)) == 2


def test_disabled_channels_print_instead_of_sending(cfg, capsys, monkeypatch):
    cfg(slack_webhook="", gchat_webhook="", smtp_user="", email_to=())
    monkeypatch.setattr(n.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no http")))
    n.send_slack("hello")
    n.send_gchat("hello")
    n.send_email("subj", "body")
    out = capsys.readouterr().out
    assert "[slack disabled]" in out and "[gchat disabled]" in out and "[email disabled]" in out


def test_send_slack_posts_payload(cfg, monkeypatch):
    cfg(slack_webhook="http://hook")
    seen = {}

    class R:
        def raise_for_status(self): pass

    monkeypatch.setattr(n.httpx, "post", lambda url, json, timeout: seen.update(url=url, json=json) or R())
    n.send_slack("hi", [{"type": "divider"}])
    assert seen["url"] == "http://hook" and seen["json"] == {"text": "hi", "blocks": [{"type": "divider"}]}


def test_notify_routes_to_configured_channels(cfg, monkeypatch):
    cfg(notify_channels=("slack", "gchat", "email"))
    calls = []
    monkeypatch.setattr(n, "send_slack", lambda t, b=None: calls.append(("slack", t)))
    monkeypatch.setattr(n, "send_gchat", lambda t: calls.append(("gchat", t)))
    monkeypatch.setattr(n, "send_email", lambda s, b: calls.append(("email", b)))
    n.notify("Title", "Body", [("Approve", "http://a", "primary")])
    assert [c[0] for c in calls] == ["slack", "gchat", "email"]
    assert "Approve: http://a" in calls[1][1] and "Approve: http://a" in calls[2][1]


def test_notify_survives_failing_channel(cfg, monkeypatch, capsys):
    cfg(notify_channels=("slack", "gchat"))
    monkeypatch.setattr(n, "send_slack", lambda *a, **k: 1 / 0)
    got = []
    monkeypatch.setattr(n, "send_gchat", lambda t: got.append(t))
    n.notify("T", "B")
    assert got and "[notify:slack] failed" in capsys.readouterr().out

"""Email channel: a reply to the card email revises the plan. Offline (fake IMAP, no SMTP)."""
from email.message import EmailMessage

import pytest

from sre_agent.core.db import get_item
from sre_agent.sre import email_reply, revision
from test_revision import cards, plan  # noqa: F401  (shared fixtures)

SENT: list = []


@pytest.fixture(autouse=True)
def reply_env(monkeypatch):
    monkeypatch.setenv("EMAIL_REPLY_ENABLED", "true")
    monkeypatch.setenv("EMAIL_REPLY_ALLOWED_SENDERS", "boss@example.com, Dev@Example.com")
    monkeypatch.setenv("IMAP_USER", "bot@example.com")
    monkeypatch.setenv("IMAP_PASSWORD", "pw")
    monkeypatch.setattr(email_reply, "send_email", lambda *a, **k: SENT.append((a, k)))
    SENT.clear()


def _reply(plan, text="Please skip b.js", sender="boss@example.com", auth="mx.example.com; spf=pass; dkim=pass",
           ref=None, version=1):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "bot@example.com", "Re: investigate: wave-node HIGH"
    ref = ref or email_reply.message_id(plan["inv"], version)
    m["In-Reply-To"], m["References"] = ref, ref
    if auth:
        m["Authentication-Results"] = auth
    m.set_content(text)
    return m.as_bytes()


def test_message_id_roundtrip_and_headers_only_when_enabled(plan, monkeypatch):
    mid = email_reply.message_id(plan["inv"], 2)
    m = email_reply.ID_RE.search(mid)
    assert m.group(1) == plan["inv"] and m.group(2) == "2" and len(m.group(3)) == 24
    assert email_reply.headers_for(plan["inv"], 1) == {"Message-ID": email_reply.message_id(plan["inv"], 1)}
    monkeypatch.setenv("EMAIL_REPLY_ENABLED", "false")
    assert email_reply.headers_for(plan["inv"], 1) == {}


def test_quotes_are_stripped_and_directives_become_fields():
    nl = chr(10)
    text = nl.join(["deny: apps/node/src/routes/b.js", "only: apps/node", "also: apps/node/src/x.js",
                    "test: null case returns 400", "ops only", "Please return 400 instead of 404.", "",
                    "On Tue, Oct 22, 2026 at 10:00 AM Bot <bot@example.com> wrote:", "> old quoted text", "> more"])
    note, fields = email_reply.parse_reply(text)
    assert note == "Please return 400 instead of 404."
    assert fields == {"deny_paths": "apps/node/src/routes/b.js", "allow_paths": "apps/node",
                      "extra_files": "apps/node/src/x.js", "extra_tests": "null case returns 400", "ops_only": "on"}
    assert email_reply.strip_quotes(nl.join(["keep", "> drop", "-----Original Message-----", "gone"])) == "keep"
    assert email_reply.parse_reply("only the words 'deny' and 'test' inside a sentence")[1] == {}


def test_a_good_reply_creates_version_two(plan, cards):
    status, detail = email_reply.process_message(
        _reply(plan, "deny: apps/node/src/routes/b.js" + chr(10) + "Please skip b.js"))
    assert status == "revised"
    v2 = get_item(detail)
    assert v2["data"]["version"] == 2 and v2["data"]["instruction"] == "Please skip b.js"
    assert v2["data"]["constraints"]["deny_paths"] == ["apps/node/src/routes/b.js"]
    assert get_item(plan["inv"])["status"] == revision.SUPERSEDED


def test_replies_from_strangers_or_unauthenticated_senders_are_refused(plan, cards, monkeypatch):
    assert email_reply.process_message(_reply(plan, sender="evil@example.com"))[0] == "refused"
    assert email_reply.process_message(_reply(plan, auth="mx; spf=fail; dkim=fail"))[0] == "refused"
    assert email_reply.process_message(_reply(plan, auth=None))[0] == "refused"
    assert email_reply.process_message(_reply(plan, auth="mx; spf=pass; dmarc=fail"))[0] == "refused"
    assert get_item(plan["inv"])["status"] == revision.AWAITING  # none of them touched the plan
    monkeypatch.setenv("EMAIL_REPLY_REQUIRE_AUTH", "false")
    assert email_reply.process_message(_reply(plan, auth=None))[0] == "revised"


def test_sender_match_ignores_case_and_display_names(plan, cards):
    assert email_reply.process_message(_reply(plan, sender="The Dev <DEV@example.com>"))[0] == "revised"


def test_unrelated_forged_or_stale_mail(plan, cards):
    assert email_reply.process_message(_reply(plan, ref="<other@mail>"))[0] == "skipped"
    forged = f"<plan.{plan['inv']}.1.{'0' * 24}@sre-agent>"
    assert email_reply.process_message(_reply(plan, ref=forged))[0] == "refused"
    assert email_reply.process_message(_reply(plan, "first"))[0] == "revised"
    status, why = email_reply.process_message(_reply(plan, "second"))  # version 1 is already replaced
    assert status == "rejected" and "newer version" in why
    args, kwargs = SENT[-1]
    assert kwargs["to"] == ["boss@example.com"] and "was not applied" in args[1]


def test_empty_reply_is_rejected_with_an_explanation(plan, cards):
    status, why = email_reply.process_message(_reply(plan, "> only a quote"))
    assert status == "rejected" and "note or set" in why and SENT


class FakeIMAP:
    log: list = []
    mails: dict = {}

    def __init__(self, host, port):
        FakeIMAP.log.append(("connect", host, port))

    def login(self, user, password):
        FakeIMAP.log.append(("login", user))

    def select(self, box):
        FakeIMAP.log.append(("select", box))

    def search(self, charset, *criteria):
        FakeIMAP.log.append(("search", criteria))
        return "OK", [b" ".join(FakeIMAP.mails)]

    def fetch(self, num, spec):
        return "OK", [(b"1 (BODY[] {1}", FakeIMAP.mails[num]), b")"]

    def store(self, num, op, flag):
        FakeIMAP.log.append(("store", num, flag))

    def logout(self):
        FakeIMAP.log.append(("logout",))


def test_poll_reads_only_plan_replies_and_marks_them_read(plan, cards):
    FakeIMAP.log, FakeIMAP.mails = [], {b"7": _reply(plan, "Please skip b.js")}
    results = email_reply.poll_once(FakeIMAP)
    assert [r[0] for r in results] == ["revised"]
    search = next(x for x in FakeIMAP.log if x[0] == "search")[1]
    assert search[0] == "UNSEEN" and '"plan."' in search  # server-side filter: only unread plan replies
    assert ("store", b"7", "\\Seen") in FakeIMAP.log and FakeIMAP.log[-1] == ("logout",)
    FakeIMAP.log, FakeIMAP.mails = [], {}
    assert email_reply.poll_once(FakeIMAP) == []


def test_card_email_carries_the_message_id_when_enabled(plan, cards, monkeypatch):
    from sre_agent.sre.incident.publish import publish_card

    sent = []
    monkeypatch.setattr("sre_agent.core.approvals.notify",
                        lambda t, b, buttons=None, email_headers=None: sent.append((b, email_headers)))
    from sre_agent.core.db import create_item

    inv_id = create_item("investigation", {"version": 1})
    publish_card(inv_id, {"service": "s", "severity": "HIGH", "error": "e"}, "sig", {
        "root_cause": "r", "affected_files": ["a.js"], "confidence": 0.8, "fix_type": "code"}, "r", 1, None)
    body, headers = sent[0]
    assert headers == {"Message-ID": email_reply.message_id(inv_id, 1)} and "reply to this email" in body

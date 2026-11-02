"""Email channel for "Request changes": a reviewer replies to the approval email.

The card email carries a Message-ID `<plan.<id>.<version>.<token>@sre-agent>` (the token is the same HMAC as the
form link). A reply's In-Reply-To / References header contains it, which identifies the plan version AND proves
the reply belongs to that card. The mailbox is read over IMAP (standard library, no new service); only unread
messages that reference a plan id are fetched.

A reply is accepted only when
    * its sender is on EMAIL_REPLY_ALLOWED_SENDERS (default: the EMAIL_TO list), and
    * the receiving server reports SPF or DKIM pass in Authentication-Results (EMAIL_REPLY_REQUIRE_AUTH=false
      relaxes this for servers that do not add the header), and
    * the plan version is still open (the same checks as the form: token, version, expiry, limit).

Reply text: quoted history is removed; optional first-column lines add structured limits:
    deny: path        only: folder        also: file        test: description        ops only
"""
import email
import imaplib
import os
import re
import time
from email.message import Message
from email.utils import parseaddr

from sre_agent.core.config import settings
from sre_agent.core.notify import approval_token, send_email
from sre_agent.core.sre_log import get_logger, step
from sre_agent.sre import revision

logger = get_logger("email_reply")
ID_RE = re.compile(r"<plan\.([0-9a-f]{10})\.(\d{1,3})\.([0-9a-f]{24})@sre-agent>")
QUOTE_START = re.compile(r"^(On .{0,300}wrote:|-{2,}\s*Original Message\s*-{2,}|_{5,}|From:\s.+|Sent from my .*)\s*$",
                         re.IGNORECASE)
DIRECTIVE = re.compile(r"^\s*(deny|don'?t touch|only|also|test)\s*:\s*(\S.*)$", re.IGNORECASE)
OPS_ONLY = re.compile(r"^\s*ops only\s*$", re.IGNORECASE)
KEY = {"deny": "deny_paths", "don't touch": "deny_paths", "dont touch": "deny_paths", "only": "allow_paths",
       "also": "extra_files", "test": "extra_tests"}
MAX_TEXT = 20_000
NL = chr(10)


def enabled() -> bool:
    return revision.reply_enabled()


def message_id(item_id: str, version: int) -> str:
    return f"<plan.{item_id}.{version}.{approval_token(item_id, f'feedback-v{version}')}@sre-agent>"


def headers_for(item_id: str, version: int) -> dict:
    """Extra headers for the card email (empty when reply-by-email is off)."""
    return {"Message-ID": message_id(item_id, version)} if enabled() else {}


def allowed_senders() -> set[str]:
    raw = os.getenv("EMAIL_REPLY_ALLOWED_SENDERS", "")
    return {a.strip().lower() for a in (raw.split(",") if raw.strip() else settings.email_to) if a.strip()}


def auth_ok(msg: Message) -> bool:
    """SPF or DKIM pass and no DMARC failure, from the receiving server's Authentication-Results header."""
    if os.getenv("EMAIL_REPLY_REQUIRE_AUTH", "true").lower() in ("false", "0", "no"):
        return True
    results = " ".join(msg.get_all("Authentication-Results") or []).lower()
    return bool(re.search(r"\b(spf|dkim)=pass\b", results)) and "dmarc=fail" not in results


def strip_quotes(text: str) -> str:
    """Keep only what the reviewer wrote: drop '>' quotes and everything from the first quote header on."""
    kept = []
    for line in text.replace("\r\n", NL).split(NL):
        if QUOTE_START.match(line.strip()):
            break
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return NL.join(kept).strip()


def parse_reply(text: str) -> tuple[str, dict]:
    """(note, fields): directive lines become structured fields, the rest is the note."""
    note, fields = [], {}
    for line in strip_quotes(text)[:MAX_TEXT].split(NL):
        m = DIRECTIVE.match(line)
        if OPS_ONLY.match(line):
            fields["ops_only"] = "on"
        elif m:
            name = KEY[m.group(1).lower()]
            fields[name] = (fields.get(name, "") + NL + m.group(2).strip()).strip()
        else:
            note.append(line)
    return NL.join(note).strip(), fields


def body_text(msg: Message) -> str:
    """The text/plain part (or tags stripped from text/html), decoded."""
    plain = html = ""
    for part in msg.walk():
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if part.get_content_subtype() == "plain" and not plain:
            plain = text
        elif part.get_content_subtype() == "html" and not html:
            html = text
    return plain or re.sub(r"<[^>]+>", " ", html)


def process_message(raw: bytes) -> tuple[str, str]:
    """Handle one email. Returns (status, detail) with status skipped | refused | revised | rejected."""
    msg = email.message_from_bytes(raw)
    refs = " ".join(str(msg.get(h, "")) for h in ("In-Reply-To", "References"))
    m = ID_RE.search(refs)
    if not m:
        return "skipped", "not a reply to a plan card"
    item_id, version, token = m.group(1), int(m.group(2)), m.group(3)
    sender = parseaddr(msg.get("From", ""))[1].lower()
    if sender not in allowed_senders():
        step(logger, "email.01.reply", "sender not allowed", level="warning", sender=sender)
        return "refused", "sender not allowed"
    if not auth_ok(msg):
        step(logger, "email.01.reply", "sender could not be authenticated", level="warning", sender=sender)
        return "refused", "sender not authenticated (no SPF or DKIM pass)"
    note, fields = parse_reply(body_text(msg))
    try:
        note, constraints = revision.begin(item_id, version, token, note, fields)
    except ValueError as e:  # fixable or already handled: tell the sender why, in one short email
        send_email(f"Re: plan change request not applied: {e}"[:150],
                   f"Your reply to plan {item_id} (version {version}) was not applied:{NL}{e}{NL}{NL}"
                   "Nothing was changed.", to=[sender])
        return "rejected", str(e)
    except (PermissionError, LookupError) as e:
        return "refused", str(e)
    new_id = revision.run_revision(item_id, note, constraints)
    step(logger, "email.01.reply", "revision accepted from email", sender=sender, investigation_id=item_id,
         new_investigation_id=new_id)
    return "revised", new_id or "failed"


def poll_once(imap_factory=None) -> list[tuple[str, str]]:
    """Read unread plan replies from the mailbox once. Returns the (status, detail) of each message."""
    host = os.getenv("IMAP_HOST", "imap.gmail.com")
    user = os.getenv("IMAP_USER") or settings.smtp_user
    password = os.getenv("IMAP_PASSWORD") or settings.smtp_password
    if not (user and password):
        return []
    results: list[tuple[str, str]] = []
    imap = (imap_factory or imaplib.IMAP4_SSL)(host, int(os.getenv("IMAP_PORT", "993")))
    try:
        imap.login(user, password)
        imap.select("INBOX")
        _, found = imap.search(None, "UNSEEN", "OR", "HEADER", "In-Reply-To", '"plan."', "HEADER", "References",
                               '"plan."')
        for num in (found[0].split() if found and found[0] else []):
            _, data = imap.fetch(num, "(BODY.PEEK[])")
            raw = next((part[1] for part in data if isinstance(part, tuple)), b"")
            results.append(process_message(raw))
            imap.store(num, "+FLAGS", "\\Seen")  # handled or refused: never read it again
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return results


def loop() -> None:
    """Poll forever (started by sre.auto when EMAIL_REPLY_ENABLED=true)."""
    interval = max(15, int(os.getenv("EMAIL_REPLY_POLL_SECONDS", "60")))
    while True:
        try:
            for status, detail in poll_once():
                step(logger, "email.02.poll", "reply processed", status=status, detail=detail[:120])
        except Exception as e:  # IMAP down, bad login: keep trying
            step(logger, "email.02.poll", "mailbox check failed", level="error", error=f"{type(e).__name__}: {e}"[:200])
        time.sleep(interval)

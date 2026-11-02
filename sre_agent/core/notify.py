"""Slack / Google Chat / Email in one place. Both hackathon ideas use this."""
import hashlib
import hmac
import os
import smtplib
from email.mime.text import MIMEText

import httpx

from .config import settings
from .sre_log import get_logger

logger = get_logger("notify")


# ---------- approval links (signed so random people can't click-approve) ----------
def approval_token(incident_id: str, stage: str) -> str:
    """HMAC-SHA256 signature (first 24 hex chars) of "<incident_id>:<stage>" using APPROVAL_SECRET.

    Only someone holding the secret can mint a valid link, so a guessed URL can't approve anything.
    """
    msg = f"{incident_id}:{stage}".encode()
    return hmac.new(settings.approval_secret.encode(), msg, hashlib.sha256).hexdigest()[:24]


def approval_url(incident_id: str, stage: str, action: str) -> str:
    """Build the clickable link served by server.py.

    Args:
        incident_id: id of the stored item (see core.db).
        stage: approval gate name, e.g. "act".
        action: "approve" or "reject".
    Returns:
        APP_BASE_URL/approve/<id>/<stage>/<action>?token=<signature>
    """
    tok = approval_token(incident_id, stage)
    return f"{settings.app_base_url}/approve/{incident_id}/{stage}/{action}?token={tok}"


def feedback_url(item_id: str, version: int) -> str:
    """Signed link to the "Request changes" form for one plan version (dies when a newer version exists)."""
    tok = approval_token(item_id, f"feedback-v{version}")
    return f"{settings.app_base_url}/feedback/{item_id}/{version}?token={tok}"


# ---------- channels ----------
def send_slack(text: str, blocks: list | None = None):
    """Post to the Slack incoming webhook. `text` is the fallback/preview, `blocks` the rich layout.

    Prints to the console instead when SLACK_WEBHOOK_URL is unset. Raises on HTTP errors.
    """
    if not settings.slack_webhook:
        return print(f"[slack disabled] {text[:200]}")
    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    httpx.post(settings.slack_webhook, json=payload, timeout=15).raise_for_status()


def send_gchat(text: str):
    """Post plain text to the Google Chat space webhook (prints to console if GCHAT_WEBHOOK_URL is unset)."""
    if not settings.gchat_webhook:
        return print(f"[gchat disabled] {text[:200]}")
    httpx.post(settings.gchat_webhook, json={"text": text}, timeout=15).raise_for_status()


def send_email(subject: str, body: str, headers: dict | None = None, to: list[str] | tuple[str, ...] | None = None):
    """Send a plain-text email to EMAIL_TO (or `to`) over SMTP (console fallback if SMTP_USER/EMAIL_TO unset).

    headers: extra MIME headers, e.g. a Message-ID that a reply can later be matched to."""
    recipients = tuple(to) if to else settings.email_to
    if not (settings.smtp_user and recipients):
        return print(f"[email disabled] {subject}")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, settings.email_from or settings.smtp_user, ", ".join(recipients)
    for name, value in (headers or {}).items():
        msg[name] = value
    if settings.smtp_port == 465:  # implicit SSL (Gmail 465); other ports use STARTTLS
        smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20)
    else:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
    with smtp as s:
        if settings.smtp_port != 465:
            s.starttls()
        s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)


# ---------- high-level helper ----------
def _slack_blocks(title: str, body: str, buttons):
    """Header + body section (+ a row of URL buttons). Truncated to Slack's length limits."""
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body[:2800]}},
    ]
    if buttons:
        interactive = bool(os.getenv("SLACK_SIGNING_SECRET") and os.getenv("SLACK_BOT_TOKEN"))
        elements = []
        for b in buttons:
            label, url, style = b[0], b[1], b[2]
            action = b[3] if len(b) > 3 else ""
            el = {"type": "button", "text": {"type": "plain_text", "text": label}}
            if action and interactive:  # opens a modal through the Slack app (needs the Request URL set up)
                el.update(action_id=action.split("|", 1)[0], value=action)
            else:
                el["url"] = url  # URL buttons need no Slack interactivity setup
            if style:
                el["style"] = style
            elements.append(el)
        blocks.append({"type": "actions", "elements": elements})
    return blocks


def notify(title: str, body: str, buttons: list[tuple] | None = None, email_headers: dict | None = None):
    """Send one message to every channel in NOTIFY_CHANNELS (slack, gchat, email).

    Args:
        title: short headline.
        body: message text (Slack markdown).
        buttons: optional [(label, url, style)], style is 'primary' | 'danger' | ''.
            Slack shows real buttons; Google Chat and email get the links as text.
    A failing channel is logged and skipped so a notification never crashes an agent.
    """
    links = "\n".join(f"{b[0]}: {b[1]}" for b in (buttons or []))
    for ch in settings.notify_channels:
        try:
            if ch == "slack":
                send_slack(f"{title}\n{body[:300]}", _slack_blocks(title, body, buttons))
            elif ch == "gchat":
                send_gchat(f"*{title}*\n{body}\n{links}")
            elif ch == "email":
                send_email(title, f"{body}\n\n{links}", **({"headers": email_headers} if email_headers else {}))
            logger.info("channel.sent", extra={"channel": ch, "title": title[:120]})
        except Exception as e:  # never let a notification crash an agent
            logger.warning("channel.failed", extra={"channel": ch, "error": str(e)[:200]})
            print(f"[notify:{ch}] failed: {e}")

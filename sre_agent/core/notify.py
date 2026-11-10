"""Slack / Google Chat / Email in one place. Both hackathon ideas use this."""
import hashlib
import hmac
import os
import smtplib
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText

import httpx

from . import slack_cards
from .config import settings
from .redact import redact
from .sre_log import get_logger

logger = get_logger("notify")


# ---------- approval links (signed so random people can't click-approve) ----------
def link_hours() -> float:
    """How long an approval link stays valid (APPROVAL_LINK_HOURS, default 7 days)."""
    try:
        return max(0.01, float(os.getenv("APPROVAL_LINK_HOURS", "168")))
    except ValueError:
        return 168.0


def legacy_links_allowed() -> bool:
    """Links minted before action-bound tokens existed (APPROVAL_ACCEPT_LEGACY=true, off by default)."""
    return os.getenv("APPROVAL_ACCEPT_LEGACY", "false").strip().lower() == "true"


def approval_token(incident_id: str, stage: str, action: str | None = None, exp: int | None = None) -> str:
    """HMAC-SHA256 signature using APPROVAL_SECRET.

    With an `action` the signature covers "<id>:<stage>:<action>:<expiry>": a link for one button cannot be edited
    into another button's link, and it stops working at `exp`. Without one (feedback links, internal use) it is the
    older "<id>:<stage>" form. Only someone holding the secret can mint a valid link.
    """
    if action is None:
        msg, size = f"{incident_id}:{stage}", 24
    else:
        msg, size = f"{incident_id}:{stage}:{action}:{int(exp or 0)}", 32
    return hmac.new(settings.approval_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()[:size]


def verify_approval(incident_id: str, stage: str, action: str, token: str, exp=None, now: float | None = None) -> None:
    """Raise PermissionError("invalid token" | "link expired") unless this link is valid for exactly this action."""
    if exp not in (None, ""):
        try:
            exp_i = int(exp)
        except (TypeError, ValueError):
            raise PermissionError("invalid token") from None
        if not hmac.compare_digest(token or "", approval_token(incident_id, stage, action, exp_i)):
            raise PermissionError("invalid token")
        if (now if now is not None else time.time()) > exp_i:
            raise PermissionError("link expired")
        return
    if legacy_links_allowed() and hmac.compare_digest(token or "", approval_token(incident_id, stage)):
        return
    raise PermissionError("invalid token")


def approval_url(incident_id: str, stage: str, action: str) -> str:
    """Build the clickable link served by server.py.

    Args:
        incident_id: id of the stored item (see core.db).
        stage: approval gate name, e.g. "act".
        action: "approve" or "reject".
    Returns:
        APP_BASE_URL/approve/<id>/<stage>/<action>?token=<signature>&exp=<unix expiry>
        Opening it shows a confirmation page; nothing happens until the Confirm button is pressed.
    """
    exp = int(time.time() + link_hours() * 3600)
    tok = approval_token(incident_id, stage, action, exp)
    return f"{settings.app_base_url}/approve/{incident_id}/{stage}/{action}?token={tok}&exp={exp}"


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
    r = httpx.post(settings.slack_webhook, json=payload, timeout=15)
    r.raise_for_status()
    return getattr(r, "status_code", 200)


def send_gchat(text: str):
    """Post plain text to the Google Chat space webhook (prints to console if GCHAT_WEBHOOK_URL is unset)."""
    if not settings.gchat_webhook:
        return print(f"[gchat disabled] {text[:200]}")
    r = httpx.post(settings.gchat_webhook, json={"text": text}, timeout=15)
    r.raise_for_status()
    return getattr(r, "status_code", 200)


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
    return True


# ---------- high-level helper ----------
def _slack_blocks(title: str, body: str, buttons):
    """Rich card for known message kinds (core.slack_cards), else header + body section; then the button row."""
    rich = slack_cards.layout(title, body)
    blocks, after = rich if rich else ([
        {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body[:2800]}},
    ], [])
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
    return blocks + after


def notify(title: str, body: str, buttons: list[tuple] | None = None, email_headers: dict | None = None):
    """Send one message to every channel in NOTIFY_CHANNELS (slack, gchat, email).

    Args:
        title: short headline.
        body: message text (Slack markdown).
        buttons: optional [(label, url, style)], style is 'primary' | 'danger' | ''.
            Slack shows real buttons; Google Chat and email get the links as text.
    A failing channel is logged and skipped so a notification never crashes an agent.
    """
    title, body = redact(title), redact(body)  # log lines are pasted into cards: never leak a secret
    links = "\n".join(f"{b[0]}: {b[1]}" for b in (buttons or []))
    deliveries = []
    for ch in settings.notify_channels:
        record = {"channel": ch, "at": datetime.now(timezone.utc).isoformat()}
        try:
            result = None
            if ch == "slack":
                result = send_slack(f"{title}\n{body[:300]}", _slack_blocks(title, body, buttons))
            elif ch == "gchat":
                result = send_gchat(f"*{title}*\n{body}\n{links}")
            elif ch == "email":
                result = send_email(title, f"{body}\n\n{links}", **({"headers": email_headers} if email_headers else {}))
            # None = the channel is not configured (the message was printed instead), so nothing was delivered or lost
            record.update(ok=None if result is None else True,
                          status=result if isinstance(result, int) and not isinstance(result, bool) else None)
            logger.info("channel.sent", extra={"channel": ch, "title": title[:120]})
        except Exception as e:  # never let a notification crash an agent
            record.update(ok=False, status=getattr(getattr(e, "response", None), "status_code", None),
                          error=redact(str(e))[:160])
            logger.warning("channel.failed", extra={"channel": ch, "error": str(e)[:200]})
            print(f"[notify:{ch}] failed: {e}")
        deliveries.append(record)
    _alert_if_undelivered(deliveries)
    return deliveries


def _alert_if_undelivered(deliveries: list[dict]) -> None:
    """Every configured channel failed: nobody saw this message, so say so (once per cooldown) on whatever still works."""
    real = [d for d in deliveries if d.get("ok") is not None]
    if not real or any(d["ok"] for d in real):
        return
    try:
        from sre_agent.sre import health

        health.alert("notify_delivery", "Notifications are not being delivered",
                     "; ".join(f"{d['channel']}: {d.get('error') or d.get('status')}" for d in real)
                     + ". Incidents are still detected and stored, but no one is being told.")
    except Exception:  # noqa: BLE001 - reporting a failure must never become another failure
        pass

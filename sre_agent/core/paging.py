"""Out-of-band delivery for things that must not sit in a chat channel unseen.

    page(title, body, severity)   PAGE_WEBHOOK_URL: a generic JSON webhook (on-call tool, SMS gateway, a Slack
                                  channel that notifies loudly). Body: {"title","body","severity","text"}.
    escalate(title, body)         ESCALATION_SLACK_WEBHOOK_URL and/or ESCALATION_EMAIL_TO: a second person or
                                  channel, used when a card has gone unanswered.

Both return False when nothing is configured and never raise (a failing pager must not crash an agent).
Text is redacted like every other outgoing message.
"""
import os

import httpx

from .redact import redact
from .sre_log import get_logger

logger = get_logger("paging")
TIMEOUT = 10


def _post(url: str, payload: dict) -> None:
    httpx.post(url, json=payload, timeout=TIMEOUT).raise_for_status()


def page(title: str, body: str, severity: str = "CRITICAL") -> bool:
    url = os.getenv("PAGE_WEBHOOK_URL", "").strip()
    if not url:
        return False
    title, body = redact(title), redact(body)
    try:
        _post(url, {"title": title, "body": body, "severity": severity,
                    "text": f"[{severity}] {title}\n{body[:1500]}"})
        logger.info("page.sent", extra={"title": title[:120], "severity": severity})
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("page.failed", extra={"error": str(e)[:200]})
        return False


def escalate(title: str, body: str) -> bool:
    title, body = redact(title), redact(body)
    sent = False
    hook = os.getenv("ESCALATION_SLACK_WEBHOOK_URL", "").strip()
    if hook:
        try:
            _post(hook, {"text": f"*{title}*\n{body[:2500]}"})
            sent = True
        except Exception as e:  # noqa: BLE001
            logger.warning("escalate.slack_failed", extra={"error": str(e)[:200]})
    to = [x.strip() for x in os.getenv("ESCALATION_EMAIL_TO", "").split(",") if x.strip()]
    if to:
        try:
            from .notify import send_email

            send_email(title, body, to=to)
            sent = True
        except Exception as e:  # noqa: BLE001
            logger.warning("escalate.email_failed", extra={"error": str(e)[:200]})
    return sent

"""Human-in-the-loop approvals.

Flow: agent calls request_approval() -> Slack/GChat/email message with signed Approve/Reject links
      -> human clicks -> server.py verifies the token -> your registered handler runs.

    @on_approval("publish")
    def handle(item_id: str, action: str) -> str:   # action = "approve" | "reject"
        ...
        return "done"                               # shown on the confirmation page
"""
import hmac
from typing import Callable

from .db import get_item, update_item
from .notify import approval_token, approval_url, notify

_handlers: dict[str, Callable[[str, str], str]] = {}
_extra_actions: dict[str, dict[str, str]] = {}  # stage -> {action: button label} beyond approve/reject


def on_approval(stage: str, extra_actions: dict[str, str] | None = None):
    """Decorator: register `fn(item_id, action)` as the handler for approval gate `stage`.

    extra_actions adds buttons beyond Approve/Reject, e.g. {"developer": "I'll fix this"}.
    """
    def deco(fn: Callable[[str, str], str]):
        _handlers[stage] = fn
        _extra_actions[stage] = dict(extra_actions or {})
        return fn
    return deco


def request_approval(item_id: str, stage: str, title: str, body: str,
                     extra_links: list[tuple] | None = None, email_headers: dict | None = None) -> None:
    """Mark the item `awaiting_<stage>` and send title/body with Approve + Reject (+ extra) buttons.

    extra_links = [(label, url, style)] appended as plain link buttons (e.g. the Request changes form)."""
    update_item(item_id, status=f"awaiting_{stage}")
    buttons = [
        ("Approve", approval_url(item_id, stage, "approve"), "primary"),
        ("Reject", approval_url(item_id, stage, "reject"), "danger"),
    ]
    buttons += [(label, approval_url(item_id, stage, action), "")
                for action, label in _extra_actions.get(stage, {}).items()]
    extra = {"email_headers": email_headers} if email_headers else {}  # only when set: keeps fakes simple
    notify(title, body, buttons=buttons + list(extra_links or []), **extra)


def handle_decision(item_id: str, stage: str, action: str, token: str) -> str:
    """Raises PermissionError / LookupError / ValueError; returns message on success."""
    if not hmac.compare_digest(token, approval_token(item_id, stage)):
        raise PermissionError("invalid token")
    if action not in ("approve", "reject", *_extra_actions.get(stage, {})):
        raise ValueError("unknown action for this approval gate")
    item = get_item(item_id)
    if item is None:
        raise LookupError("unknown item")
    if item["status"] != f"awaiting_{stage}":  # makes links single-use
        raise ValueError(f"already handled (status={item['status']})")
    handler = _handlers.get(stage)
    if handler is None:
        raise LookupError(f"no handler registered for stage '{stage}'")
    update_item(item_id, status=f"{stage}_{action}d")
    return handler(item_id, action)

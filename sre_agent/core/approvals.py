"""Human-in-the-loop approvals.

Flow: agent calls request_approval() -> Slack/GChat/email message with signed Approve/Reject links
      -> human clicks -> server.py verifies the token -> your registered handler runs.

    @on_approval("publish")
    def handle(item_id: str, action: str) -> str:   # action = "approve" | "reject"
        ...
        return "done"                               # shown on the confirmation page
"""
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .db import get_item, update_item
from .notify import approval_url, notify, verify_approval

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
                     extra_links: list[tuple] | None = None, email_headers: dict | None = None,
                     hide: tuple[str, ...] = (), labels: dict[str, str] | None = None) -> None:
    """Mark the item `awaiting_<stage>` and send title/body with Approve + Reject (+ extra) buttons.

    extra_links = [(label, url, style)] appended as plain link buttons (e.g. the Request changes form).
    hide = built-in buttons not to show (e.g. ("approve", "reject") on a notice-only card).
    labels = rename a built-in button, e.g. {"approve": "Retry the fix"}."""
    update_item(item_id, status=f"awaiting_{stage}")
    buttons = fresh_buttons(item_id, stage, hide=hide, labels=labels)
    extra = {"email_headers": email_headers} if email_headers else {}  # only when set: keeps fakes simple
    deliveries = notify(title, body, buttons=buttons + list(extra_links or []), **extra)
    if isinstance(deliveries, list):
        update_item(item_id, delivery=deliveries)  # the proof that this card reached a channel
    return deliveries


def _button(label: str, item_id: str, stage: str, action: str, style: str) -> tuple:
    """(label, link, style, slack value). In Slack with the app configured the button decides directly (the value
    carries the same signed, expiring token); otherwise it opens the link, which shows a confirmation page."""
    url = approval_url(item_id, stage, action)
    q = parse_qs(urlparse(url).query)
    return (label, url, style, f"decide:{action}|{item_id}|{stage}|{q['exp'][0]}|{q['token'][0]}")


def fresh_buttons(item_id: str, stage: str, hide: tuple[str, ...] = (), labels: dict[str, str] | None = None) -> list[tuple]:
    """The decision buttons for an item, with newly signed links (so a reminder never carries an expired link)."""
    names = labels or {}
    buttons = [_button(names.get(action, default), item_id, stage, action, style)
               for action, default, style in (("approve", "Approve", "primary"), ("reject", "Reject", "danger"))
               if action not in hide]
    return buttons + [_button(label, item_id, stage, action, "")
                      for action, label in _extra_actions.get(stage, {}).items()]


def check_decision(item_id: str, stage: str, action: str, token: str, exp=None) -> dict:
    """Everything handle_decision checks, without doing anything. Returns the item.
    Raises PermissionError / LookupError / ValueError. The confirmation page uses this, so merely opening a link
    (an email scanner, a link preview) can never trigger the action."""
    verify_approval(item_id, stage, action, token, exp)
    if action not in ("approve", "reject", *_extra_actions.get(stage, {})):
        raise ValueError("unknown action for this approval gate")
    item = get_item(item_id)
    if item is None:
        raise LookupError("unknown item")
    if item["status"] != f"awaiting_{stage}":
        raise ValueError(f"already handled (status={item['status']})")
    return item


def handle_decision(item_id: str, stage: str, action: str, token: str, exp=None) -> str:
    """Raises PermissionError / LookupError / ValueError; returns message on success."""
    verify_approval(item_id, stage, action, token, exp)
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

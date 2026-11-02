"""Slack channel for "Request changes": a card button opens a modal, its submission feeds sre.revision.

Needs a Slack app (SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET) whose Interactivity Request URL is
    POST {APP_BASE_URL}/slack/interactions
Every request is verified with the signing secret (v0 HMAC of "v0:<timestamp>:<raw body>", constant-time
compare, timestamps older than five minutes refused). Without the two env vars the endpoint answers 404 and
the card keeps its plain "Request changes" web link, so nothing here is required.

Slack gives 3 seconds: views.open and the submission check are quick; the revision itself runs after the reply.
"""
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from sre_agent.core.sre_log import get_logger, step
from sre_agent.sre import revision

logger = get_logger("slack")
router = APIRouter()
SLACK_API = "https://slack.com/api"
MAX_AGE_SECONDS = 300
CALLBACK = "request_changes_modal"
ACTION = "request_changes"
FIELDS = (  # (block id, label, multiline hint)
    ("note", "What should change?", False),
    ("deny_paths", "Files or folders NOT to touch (one per line)", True),
    ("allow_paths", "Only edit under these folders (one per line)", True),
    ("extra_files", "Also fix these files (one per line, must exist)", True),
    ("extra_tests", "Tests that must be added (one per line)", True),
)


def configured() -> bool:
    return bool(os.getenv("SLACK_SIGNING_SECRET") and os.getenv("SLACK_BOT_TOKEN"))


def verify_signature(raw: bytes, timestamp: str, signature: str, secret: str, now: float | None = None) -> bool:
    """Slack request signing: reject stale timestamps and compare HMACs in constant time."""
    try:
        age = abs((now if now is not None else time.time()) - int(timestamp))
    except (TypeError, ValueError):
        return False
    if age > MAX_AGE_SECONDS or not signature:
        return False
    base = b"v0:" + timestamp.encode() + b":" + raw
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def allowed_user(user_id: str) -> bool:
    """SLACK_ALLOWED_USERS (comma-separated Slack user ids). Empty list = anyone who can see the card."""
    allowed = [u.strip() for u in os.getenv("SLACK_ALLOWED_USERS", "").split(",") if u.strip()]
    return not allowed or user_id in allowed


def _mrkdwn(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def modal_view(item: dict, version: int, token: str, error: str = "") -> dict:
    """The Block Kit modal. private_metadata carries the signed plan reference back on submit."""
    d = item["data"]
    files = ", ".join(d.get("affected_files") or []) or "none (operational fix)"
    summary = (f"*{_mrkdwn(d.get('service', ''))}* · {_mrkdwn(d.get('error', ''))}\n"
               f"Plan version {version} · fix type {_mrkdwn(d.get('fix_type', '?'))} · files: {_mrkdwn(files)}\n"
               f"{_mrkdwn(d.get('summary', ''))}")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": summary[:2900]}}]
    if error:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f":warning: {_mrkdwn(error)}"}})
    for block_id, label, multiline in FIELDS:
        blocks.append({"type": "input", "block_id": block_id, "optional": block_id != "note",
                       "label": {"type": "plain_text", "text": label},
                       "element": {"type": "plain_text_input", "action_id": "v", "multiline": multiline or block_id == "note",
                                   "max_length": revision.note_limit() if block_id == "note" else 1000}})
    blocks.append({"type": "input", "block_id": "ops_only", "optional": True,
                   "label": {"type": "plain_text", "text": "Options"},
                   "element": {"type": "checkboxes", "action_id": "v", "options": [
                       {"text": {"type": "plain_text", "text": "Operational fix only, do not change code"}, "value": "on"}]}})
    return {"type": "modal", "callback_id": CALLBACK, "private_metadata": f"{item['id']}|{version}|{token}",
            "title": {"type": "plain_text", "text": "Request changes"},
            "submit": {"type": "plain_text", "text": "Send"}, "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": blocks}


def open_modal(trigger_id: str, view: dict) -> None:
    """views.open; the trigger_id is only valid for 3 seconds, so this is called before anything slow."""
    r = httpx.post(f"{SLACK_API}/views.open", timeout=2.5,
                   headers={"Authorization": "Bearer " + os.getenv("SLACK_BOT_TOKEN", "")},
                   json={"trigger_id": trigger_id, "view": view})
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"views.open failed: {body.get('error')}")


def _tell(payload: dict, text: str) -> None:
    """A private note to the person who clicked (through the payload's response_url)."""
    url = payload.get("response_url")
    if url:
        try:
            httpx.post(url, json={"response_type": "ephemeral", "text": text, "replace_original": False}, timeout=2.5)
        except httpx.HTTPError:
            pass


def values_of(view: dict) -> dict:
    """view.state.values -> {block id: text, or 'on' for a ticked checkbox}."""
    out = {}
    for block_id, actions in ((view.get("state") or {}).get("values") or {}).items():
        act = actions.get("v") or {}
        if act.get("type") == "checkboxes":
            out[block_id] = "on" if act.get("selected_options") else ""
        else:
            out[block_id] = act.get("value") or ""
    return out


def on_block_action(payload: dict) -> None:
    user = (payload.get("user") or {}).get("id", "")
    action = next((a for a in payload.get("actions") or [] if a.get("action_id") == ACTION), None)
    if not action:
        return
    if not allowed_user(user):
        step(logger, "slack.01.action", "user not allowed", level="warning", user=user)
        return _tell(payload, "You are not on the list of people who may request changes.")
    try:
        _, item_id, version, token = (action.get("value") or "").split("|", 3)
        item = revision.check_link(item_id, int(version), token)
        open_modal(payload.get("trigger_id", ""), modal_view(item, int(version), token))
        step(logger, "slack.01.action", "modal opened", user=user, investigation_id=item_id, version=int(version))
    except (ValueError, PermissionError, LookupError) as e:
        _tell(payload, f"Cannot open the form: {e}")
    except (RuntimeError, httpx.HTTPError) as e:
        step(logger, "slack.01.action", "could not open the modal", level="error", error=str(e)[:200])


def on_view_submission(payload: dict, background: BackgroundTasks) -> dict:
    """Returns the body Slack expects: errors shown inside the modal, or clear it on success."""
    view = payload.get("view") or {}
    user = (payload.get("user") or {}).get("id", "")
    if view.get("callback_id") != CALLBACK:
        return {}
    if not allowed_user(user):
        return {"response_action": "errors", "errors": {"note": "You are not allowed to request changes."}}
    try:
        item_id, version, token = (view.get("private_metadata") or "").split("|", 2)
        fields = values_of(view)
        note, constraints = revision.begin(item_id, int(version), token, fields.get("note", ""), fields)
    except (ValueError, PermissionError, LookupError) as e:
        return {"response_action": "errors", "errors": {"note": str(e)[:300]}}
    background.add_task(revision.run_revision, item_id, note, constraints)
    step(logger, "slack.02.submit", "revision accepted from Slack", user=user, investigation_id=item_id)
    return {"response_action": "clear"}


@router.post("/slack/interactions")
async def interactions(request: Request, background: BackgroundTasks):
    secret = os.getenv("SLACK_SIGNING_SECRET", "")
    if not secret:
        raise HTTPException(404, "Slack is not configured")
    raw = await request.body()
    if not verify_signature(raw, request.headers.get("x-slack-request-timestamp", ""),
                            request.headers.get("x-slack-signature", ""), secret):
        raise HTTPException(401, "bad signature")
    try:
        payload = json.loads(parse_qs(raw.decode("utf-8", "replace"))["payload"][0])
    except (KeyError, IndexError, ValueError):
        raise HTTPException(400, "no payload")
    kind = payload.get("type")
    if kind == "block_actions":
        on_block_action(payload)
        return Response(status_code=200)
    if kind == "view_submission":
        return JSONResponse(on_view_submission(payload, background))
    return Response(status_code=200)

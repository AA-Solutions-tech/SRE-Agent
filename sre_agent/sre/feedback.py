"""The "Request changes" form: GET shows it, POST submits it (both token-checked, see sre/revision.py).

    GET  /feedback/{plan_id}/{version}?token=...   the form
    POST /feedback/{plan_id}/{version}             note + optional fields (urlencoded body, token inside)

No new dependency: the urlencoded body is parsed with the standard library. Every dynamic value that is
printed is HTML-escaped. The revision itself runs after the response, in the background.
"""
import html
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import HTMLResponse

from sre_agent.core.sre_log import get_logger, step
from sre_agent.sre import revision

logger = get_logger("feedback")
router = APIRouter()
MAX_BODY = 20_000
CSS = ("body{font:15px/1.5 system-ui,sans-serif;max-width:680px;margin:32px auto;padding:0 16px;color:#1d2433}"
       "label{display:block;font-weight:600;margin-top:16px}small{color:#5b6475;font-weight:400}"
       "textarea,input[type=text]{width:100%;box-sizing:border-box;padding:8px;font:inherit;margin-top:4px}"
       "button{margin-top:20px;padding:10px 18px;font:inherit;font-weight:600;cursor:pointer}"
       ".box{background:#f3f5f9;border-radius:8px;padding:12px 16px}.err{color:#a4262c;font-weight:600}")


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
                        f"initial-scale=1'><title>{html.escape(title)}</title><style>{CSS}</style>{body}", status)


def _form(item: dict, version: int, token: str, note: str = "", fields: dict | None = None, error: str = "") -> str:
    d, f = item["data"], fields or {}
    esc = html.escape
    files = ", ".join(d.get("affected_files") or []) or "none (operational fix)"
    return (
        f"<h2>Request changes to the plan</h2><div class='box'><b>{esc(str(d.get('service', '')))}</b> · "
        f"{esc(str(d.get('error', '')))}<br>Plan version {version} · fix type {esc(str(d.get('fix_type', '?')))} · "
        f"confidence {round(float(d.get('confidence') or 0) * 100)}%<br>Files to edit: {esc(files)}<br>"
        f"{esc(str(d.get('summary', '')))}</div>"
        + (f"<p class='err'>{esc(error)}</p>" if error else "")
        + f"<form method='post'><input type='hidden' name='token' value='{esc(token)}'>"
        f"<label>What should change? <small>(up to {revision.note_limit()} characters)</small>"
        f"<textarea name='note' rows='6'>{esc(note)}</textarea></label>"
        f"<label>Files or folders NOT to touch <small>(one per line)</small>"
        f"<textarea name='deny_paths' rows='2'>{esc(str(f.get('deny_paths', '')))}</textarea></label>"
        f"<label>Only edit under these folders <small>(one per line, optional)</small>"
        f"<textarea name='allow_paths' rows='2'>{esc(str(f.get('allow_paths', '')))}</textarea></label>"
        f"<label>Also fix these files <small>(one per line, must exist in the repository)</small>"
        f"<textarea name='extra_files' rows='2'>{esc(str(f.get('extra_files', '')))}</textarea></label>"
        f"<label>Tests that must be added <small>(one per line)</small>"
        f"<textarea name='extra_tests' rows='2'>{esc(str(f.get('extra_tests', '')))}</textarea></label>"
        f"<label><input type='checkbox' name='ops_only'{' checked' if f.get('ops_only') else ''}> "
        f"Operational fix only, do not change code</label>"
        f"<button type='submit'>Send for a new plan version</button></form>"
        f"<p><small>{revision.revisions_left(version)} revision(s) left for this issue. You will get a new email "
        f"with the updated plan and what changed.</small></p>"
    )


def _refusal(e: Exception) -> HTMLResponse:
    if isinstance(e, PermissionError):
        return _page("Not allowed", "<h2>Not allowed</h2><p>This link is not valid.</p>", 403)
    if isinstance(e, LookupError):
        return _page("Not found", f"<h2>Not found</h2><p>{html.escape(str(e))}</p>", 404)
    return _page("Cannot continue", f"<h2>Cannot continue</h2><p>{html.escape(str(e))}</p>", 409)


@router.get("/feedback/{item_id}/{version}", response_class=HTMLResponse)
def feedback_form(item_id: str, version: int, token: str = Query(...)):
    try:
        item = revision.check_link(item_id, version, token)
    except (PermissionError, LookupError, ValueError) as e:
        return _refusal(e)
    return _page("Request changes", _form(item, version, token))


@router.post("/feedback/{item_id}/{version}", response_class=HTMLResponse)
async def feedback_submit(item_id: str, version: int, request: Request, background: BackgroundTasks):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return _page("Too large", "<h2>Too large</h2><p>The request is too big.</p>", 413)
    fields = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}
    token = fields.get("token", "")
    try:
        note, constraints = revision.begin(item_id, version, token, fields.get("note", ""), fields)
    except ValueError as e:  # a fixable problem: show the form again with the reason and what was typed
        try:
            item = revision.check_link(item_id, version, token)
        except (PermissionError, LookupError, ValueError):
            return _refusal(e)
        return _page("Request changes", _form(item, version, token, fields.get("note", ""), fields, str(e)), 400)
    except (PermissionError, LookupError) as e:
        return _refusal(e)
    background.add_task(revision.run_revision, item_id, note, constraints)
    step(logger, "feedback.01.accepted", "revision accepted, running in the background", investigation_id=item_id,
         version=version)
    return _page("Thank you", "<h2>Thank you</h2><p>The plan is being revised. You will get a new email with the "
                              "updated plan, what changed, and fresh Approve, Reject and Request changes links.</p>")

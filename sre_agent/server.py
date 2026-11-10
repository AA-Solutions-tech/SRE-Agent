"""FastAPI app: approval links + health + manual agent trigger.

    sre-agent serve                        (installed)
    uvicorn server:app --reload --port 8000  (dev, from the repo root)
Expose it to Slack/phones with e.g. `ngrok http 8000` and set APP_BASE_URL to the public URL.
"""
import html

from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from sre_agent.agents import agent_names, load_agent, load_all
from sre_agent import __version__
from sre_agent.core.approvals import check_decision, handle_decision
from sre_agent.core.db import update_item
from sre_agent.core.auth import require_api_key
from sre_agent.core.sre_log import get_logger
from sre_agent.sre.endpoints import router as sre_router
from sre_agent.sre.feedback import router as feedback_router
from sre_agent.sre import fixfail, ship  # noqa: F401  (registers the ship_fix and fix_retry approval gates)
from sre_agent.sre.slack_app import router as slack_router

logger = get_logger("server")

app = FastAPI(title="SRE Agent", version=__version__)
app.include_router(sre_router, dependencies=[Depends(require_api_key)])
app.include_router(feedback_router)
app.include_router(slack_router)
load_all()  # registers every agent's @on_approval handler


@app.on_event("startup")
def _auto_start():
    """SRE_AUTO_RUN=true -> start the monitor watcher; signals flow to supervisor/incident by themselves."""
    from sre_agent.sre import llm_check
    from sre_agent.sre.auto import start
    llm_check.start_background()  # a rejected key / model / parameter is reported once, loudly, not card by card
    start()


@app.get("/health")
def health():
    return {"ok": True, "version": __version__, "agents": agent_names()}


PAGE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"}


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                        f"<body style='font-family:system-ui,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem'>"
                        f"{body}</body>", status_code=status, headers=PAGE_HEADERS)


def _describe(item: dict) -> str:
    d = item["data"]
    bits = [d.get("service"), d.get("error") or d.get("summary")]
    return " · ".join(html.escape(str(b)) for b in bits if b) or html.escape(item["kind"])


@app.get("/approve/{item_id}/{stage}/{action}", response_class=HTMLResponse)
def approve_page(item_id: str, stage: str, action: str, token: str = Query(...), exp: str | None = Query(None)):
    """Opening a link only SHOWS what it would do. Email scanners and link previews fetch links; they must not decide."""
    try:
        item = check_decision(item_id, stage, action, token, exp)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    target = f"/approve/{quote(item_id)}/{quote(stage)}/{quote(action)}?token={quote(token)}" + (f"&exp={quote(exp)}" if exp else "")
    return _page(f"<h2>Confirm: {html.escape(action)}</h2><p>{_describe(item)}</p>"
                 f"<form method=post action='{html.escape(target)}'><button type=submit style='font-size:1.1rem;padding:.6rem 1.2rem'>"
                 f"Confirm {html.escape(action)}</button></form><p style='color:#666'>Nothing happens until you press the button.</p>")


@app.post("/approve/{item_id}/{stage}/{action}", response_class=HTMLResponse)
def approve(item_id: str, stage: str, action: str, request: Request, token: str = Query(...), exp: str | None = Query(None)):
    try:
        msg = handle_decision(item_id, stage, action, token, exp)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    who = {"ip": request.client.host if request.client else "?", "agent": request.headers.get("user-agent", "")[:120],
           "at": datetime.now(timezone.utc).isoformat()}
    try:
        update_item(item_id, decided_from=who)  # the link carries no identity; this is the audit trail
    except KeyError:
        pass
    logger.info("http.approve", extra={"item_id": item_id, "stage": stage, "action": action, "from": who["ip"]})
    return _page(f"<h2>{html.escape(action.title())}d</h2><p>{html.escape(msg)}</p>")


@app.post("/run/{agent}", dependencies=[Depends(require_api_key)])
def run_agent(agent: str, payload: dict | None = None):
    """Trigger an agent over HTTP (handy for webhooks): POST /run/hello  {"question": "..."}"""
    if agent not in agent_names():
        raise HTTPException(404, "unknown agent")
    try:
        result = load_agent(agent).run(**(payload or {}))
    except ValueError as e:  # bad reference (unknown signal id, missing arg)
        raise HTTPException(404, str(e))
    logger.info("http.run", extra={"agent": agent, "ok": True})
    return {"result": result}

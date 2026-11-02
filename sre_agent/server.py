"""FastAPI app: approval links + health + manual agent trigger.

    sre-agent serve                        (installed)
    uvicorn server:app --reload --port 8000  (dev, from the repo root)
Expose it to Slack/phones with e.g. `ngrok http 8000` and set APP_BASE_URL to the public URL.
"""
import html

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from sre_agent.agents import agent_names, load_agent, load_all
from sre_agent import __version__
from sre_agent.core.approvals import handle_decision
from sre_agent.core.auth import require_api_key
from sre_agent.core.sre_log import get_logger
from sre_agent.sre.endpoints import router as sre_router
from sre_agent.sre.feedback import router as feedback_router
from sre_agent.sre import ship  # noqa: F401  (registers the ship_fix approval gate)
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
    from sre_agent.sre.auto import start
    start()


@app.get("/health")
def health():
    return {"ok": True, "version": __version__, "agents": agent_names()}


@app.get("/approve/{item_id}/{stage}/{action}", response_class=HTMLResponse)
def approve(item_id: str, stage: str, action: str, token: str = Query(...)):
    try:
        msg = handle_decision(item_id, stage, action, token)
    except PermissionError:
        raise HTTPException(403, "invalid token")
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    logger.info("http.approve", extra={"item_id": item_id, "stage": stage, "action": action})
    return f"<h2>{action.title()}d</h2><p>{html.escape(msg)}</p>"


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

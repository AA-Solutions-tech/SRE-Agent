"""Clef adapter: Cloudflare Workers AI decision-making model over HTTPS.

Calls POST /client/v4/accounts/{id}/ai/run/{model} with a prompt built
from the signal + installed agents, and parses the model's JSON reply
into a Route. Deliberately thin: build request, parse response, validate
shape. All policy (installed-only enforcement, fallback to the rules
router on any failure) lives in agent.py, not here.

Env:
    CLOUDFLARE_ACCOUNT_ID  Cloudflare account id (required)
    CLOUDFLARE_API_TOKEN   bearer token, Workers AI run scope (required)
    CLEF_MODEL             e.g. @cf/cloudflare/clef (required)
    CLEF_TIMEOUT           seconds per request (default 10)
    CLEF_MIN_CONFIDENCE    below this -> caller falls back to rules (default 0.5)

Any transport problem, Cloudflare error envelope, or malformed model
reply raises RouterError so the supervisor can fall back deterministically.
"""
import json
import os

import httpx
from httpx import HTTPStatusError  # bound directly: survives tests that mock clef.httpx

from sre_agent.core.sre_log import get_logger
from .router import Route, RouterError

logger = get_logger("clef")

API_BASE = "https://api.cloudflare.com/client/v4"
CAPABLE = ("monitor", "incident", "coding")


def _config() -> tuple[str, str, str, float, float]:
    account = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    model = os.getenv("CLEF_MODEL", "").strip()
    try:
        timeout = float(os.getenv("CLEF_TIMEOUT", "10"))
    except ValueError:
        timeout = 10.0
    try:
        min_conf = float(os.getenv("CLEF_MIN_CONFIDENCE", "0.5"))
    except ValueError:
        min_conf = 0.5
    return account, token, model, timeout, min_conf


def min_confidence() -> float:
    return _config()[4]


def endpoint() -> str:
    account, _, model, _, _ = _config()
    return f"{API_BASE}/accounts/{account}/ai/run/{model}"


def build_prompt(signal: dict, available: list[str]) -> str:
    capable = [a for a in available if a in CAPABLE]
    return (
        "You route SRE signals to exactly one agent. Reply with JSON only, "
        'no prose: {"agent": <name or null>, "confidence": <0-1>, "reason": <short>}.\n'
        f"Available agents: {json.dumps(capable)}\n"
        "Reply agent=null when none of them can handle the signal.\n"
        f"Signal: {json.dumps({k: signal.get(k) for k in ('service', 'severity', 'error', 'evidence', 'ts')})}"
    )


def parse_reply(text: str) -> Route:
    """Extract the first JSON object from a model reply and validate it."""
    start = text.find("{")
    if start < 0:
        raise RouterError(f"clef reply has no JSON object: {text!r:.150}")
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as e:
        raise RouterError(f"clef reply is not valid JSON: {text!r:.150}") from e
    if not isinstance(data, dict):
        raise RouterError(f"clef reply is not an object: {text!r:.150}")
    agent = data.get("agent")
    if agent is not None and not isinstance(agent, str):
        raise RouterError(f"clef reply has bad agent: {text!r:.150}")
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        raise RouterError(f"clef reply has bad confidence: {text!r:.150}")
    reason = str(data.get("reason", "clef decision"))
    return Route(agent=agent, confidence=confidence, reason=f"clef: {reason}")


def decide(signal: dict, available: list[str]) -> Route:
    account, token, model, timeout, _ = _config()
    missing = [k for k, v in (
        ("CLOUDFLARE_ACCOUNT_ID", account),
        ("CLOUDFLARE_API_TOKEN", token),
        ("CLEF_MODEL", model),
    ) if not v]
    if missing:
        raise RouterError(f"clef not configured (missing {', '.join(missing)})")
    url = endpoint()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    try:
        resp = httpx.post(url, json={"prompt": build_prompt(signal, available)},
                          headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except RouterError:
        raise
    except HTTPStatusError as e:
        # status only: never leak endpoint/account details into results
        raise RouterError(f"clef rejected request (HTTP {e.response.status_code})") from e
    except Exception as e:
        raise RouterError(f"clef request failed ({type(e).__name__})") from e
    if not isinstance(data, dict):
        raise RouterError(f"clef returned non-object envelope: {data!r:.150}")
    if not data.get("success", False):
        raise RouterError(f"clef error envelope: {data.get('errors', data)!r:.200}")
    try:
        text = data["result"]["response"]
    except (KeyError, TypeError):
        raise RouterError(f"clef envelope has no result.response: {data!r:.200}")
    if not isinstance(text, str):
        raise RouterError(f"clef response is not text: {text!r:.150}")
    route = parse_reply(text)
    logger.info("clef.called", extra={"outcome": "ok", "target": route.get("agent"),
                                      "confidence": route.get("confidence")})
    return route

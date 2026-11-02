"""API-key check for the HTTP API (/run, /agents, /signals, /issues).

Clients send `X-API-Key: <SRE_API_KEY>` (or `Authorization: Bearer <key>`). Approval/feedback links carry their own
HMAC token and Slack requests are verified with the Slack signing secret, so those routes do not use this.
An empty SRE_API_KEY leaves the API open, which is only meant for local development; packaged installs generate one.
"""
import hmac

from fastapi import Header, HTTPException

from .config import settings


def _presented(x_api_key: str | None, authorization: str | None) -> str:
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def require_api_key(x_api_key: str | None = Header(default=None),
                    authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: 401 unless the request carries the configured key."""
    expected = settings.api_key
    if not expected:
        return
    got = _presented(x_api_key, authorization)
    if not got or not hmac.compare_digest(got.encode(), expected.encode()):
        raise HTTPException(401, "missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})

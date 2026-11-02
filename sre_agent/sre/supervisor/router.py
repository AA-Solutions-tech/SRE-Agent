"""Router contract shared by Clef (Cloudflare decision model) and the rules fallback.

Both routers implement `decide(signal, available) -> Route`. The supervisor
always passes the final answer through `enforce()`, so a removed agent is
never routable no matter what a model says.
"""
from typing import Protocol


class Route(dict):
    """Routing decision: {"agent": str | None, "confidence": float, "reason": str}.

    `agent` is None when no capable agent is installed.
    """


class Router(Protocol):
    def decide(self, signal: dict, available: list[str]) -> Route: ...


class RouterError(RuntimeError):
    """A router cannot decide (e.g. Clef unreachable or bad response)."""


def enforce(route: Route, available: list[str]) -> Route:
    """Hard guard: constrain any routing decision to installed agents."""
    agent = route.get("agent")
    if agent not in available:
        return Route(
            agent=None,
            confidence=0.0,
            reason=f"routed agent {agent!r} is not installed",
        )
    return route

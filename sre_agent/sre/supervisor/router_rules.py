"""Deterministic fallback router (offline, no network, no cost).

Severity decides the preference order; only installed agents from the
capable set (monitor, incident, coding) are ever picked. Anything else
(agent missing entirely) yields agent=None so the supervisor degrades
with a clear notice instead of failing.
"""
from .router import Route

CAPABLE = ("monitor", "incident", "coding")

PREFERENCE = {
    "CRITICAL": ("incident", "coding", "monitor"),
    "HIGH": ("incident", "coding", "monitor"),
    "MEDIUM": ("incident", "monitor", "coding"),
    "LOW": ("monitor", "incident", "coding"),
}


def decide(signal: dict, available: list[str]) -> Route:
    severity = signal.get("severity", "LOW")
    for name in PREFERENCE.get(severity, PREFERENCE["LOW"]):
        if name in available and name in CAPABLE:
            return Route(
                agent=name,
                confidence=1.0,
                reason=f"rules: severity {severity} -> {name}",
            )
    return Route(
        agent=None,
        confidence=0.0,
        reason="rules: no capable agent installed (need monitor, incident, or coding)",
    )

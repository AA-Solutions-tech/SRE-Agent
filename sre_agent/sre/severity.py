"""Severity beyond the log level: rules, important services, quiet hours.

Log level alone makes every ERROR "HIGH": a bot probing for /wp-login.php looks the same as a crashed payment
service. apply() decides the real severity, first match wins:

    1. CRITICAL_SERVICES / CRITICAL_PATTERNS            business-critical: CRITICAL (also pages)
    2. rules file (SEVERITY_RULES_FILE, default <SRE_HOME>/severity.json)
       {"rules": [{"match": "regex on error + evidence", "service": "optional", "severity": "LOW"}]}
    3. SERVICE_SEVERITY  "bot-probe=LOW,internal-tool=MEDIUM"   a service's own default
    4. built-in rules (SEVERITY_DEFAULTS=false turns them off):
         handled client errors on an inbound request (4xx) and bot-probe paths   -> LOW
         expected CORS rejections                                                -> LOW
         rate limits, external services, bad data, security probes               -> MEDIUM
         anything else (5xx, crashes, config, credentials, database ...)         -> unchanged (HIGH)
LOW issues are counted quietly and reported in a digest, never as a card, until LOW_ESCALATE_COUNT repeats.

Quiet hours: signals below QUIET_MIN_SEVERITY are stored and counted but their card waits until the quiet time ends.
    QUIET_HOURS  local time range, e.g. 22:00-07:00 (empty = never quiet)    QUIET_MIN_SEVERITY (default HIGH)
"""
import json
import os
import re
from datetime import datetime, time
from pathlib import Path

from sre_agent.core.config import settings

RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
_HOURS_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def _csv(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


def is_critical(signal: dict) -> bool:
    if (signal.get("service") or "").lower() in {s.lower() for s in _csv("CRITICAL_SERVICES")}:
        return True
    text = "\n".join([str(signal.get("error") or ""), *[str(e) for e in (signal.get("evidence") or [])[:20]]])
    for pattern in _csv("CRITICAL_PATTERNS"):
        try:
            if re.search(pattern, text, re.I):
                return True
        except re.error:
            continue  # a bad pattern never blocks a signal
    return False


_NOT_FOUND_RE = re.compile(r"not found|\b404\b|forbidden|unauthori[sz]ed|cannot (?:get|post)|no route|invalid route", re.I)
_NOISE_PATH_RE = re.compile(r"wp-login|wp-admin|wp-content|xmlrpc|phpmyadmin|/\.env\b|/\.git\b|\.php\b|/admin\b|/cgi-bin|"
                            r"/vendor/|/actuator|favicon\.ico|robots\.txt|apple-touch-icon|sitemap\.xml|\.asp", re.I)
_CORS_RE = re.compile(r"\bcors\b", re.I)
_CORS_EXPECTED_RE = re.compile(r"reject|not allowed|blocked|denied|origin", re.I)
_VERB_RE = re.compile(r"^\s*(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+\S+", re.I)
_REQUEST_KEYS = ("method", "url", "path", "route", "originalUrl", "req", "request", "kind")
CATEGORY_SEVERITY = {"rate_limit": "MEDIUM", "external": "MEDIUM", "data": "MEDIUM", "security": "MEDIUM"}
VALID = set(RANK)
_rules_cache: dict = {"key": None, "rules": []}


def _text(signal: dict) -> str:
    return "\n".join([str(signal.get("error") or ""), *[str(e) for e in (signal.get("evidence") or [])[:20]]])


def _inbound_status_codes(signal: dict) -> list[int]:
    """HTTP status codes of INBOUND requests in the evidence (access-log style lines). A 401 from a service this app
    calls is not a client error, so a status only counts when the line is shaped like a request to this app."""
    codes = []
    for ev in (signal.get("evidence") or [])[:20]:
        text = str(ev)
        if text.lstrip().startswith("{"):
            try:
                obj = json.loads(text)
            except ValueError:
                continue
            if isinstance(obj, dict) and any(k in obj for k in _REQUEST_KEYS):
                for key in ("statusCode", "status", "status_code"):
                    try:
                        code = int(obj.get(key))
                    except (TypeError, ValueError):
                        continue
                    if 100 <= code <= 599:
                        codes.append(code)
                        break
        elif _VERB_RE.match(text):
            codes += [int(c) for c in re.findall(r"\b([1-5]\d\d)\b", text)][-1:]
    return codes


def default_severity(signal: dict) -> str | None:
    """The built-in rules (see the module docstring). None = leave the severity as the log level set it."""
    if os.getenv("SEVERITY_DEFAULTS", "true").strip().lower() == "false":
        return None
    text = _text(signal)
    codes = _inbound_status_codes(signal)
    if codes:
        return None if max(codes) >= 500 else "LOW"
    if _NOISE_PATH_RE.search(text) and _NOT_FOUND_RE.search(text):
        return "LOW"
    if _CORS_RE.search(text) and _CORS_EXPECTED_RE.search(text):
        return "LOW"
    return CATEGORY_SEVERITY.get(str(signal.get("category")))


def _rules_path() -> Path:
    return Path(os.getenv("SEVERITY_RULES_FILE", "").strip() or settings.data_dir / "severity.json")


def _load_rules() -> list[tuple[re.Pattern, str, str]]:
    """[(pattern, service or "", severity)] from the rules file, re-read when it changes. Bad entries are skipped."""
    path = _rules_path()
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return []
    if _rules_cache["key"] == key:
        return _rules_cache["rules"]
    rules = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("rules", []) if isinstance(data, dict) else data
    except (OSError, ValueError, AttributeError):
        entries = []
    for raw in entries if isinstance(entries, list) else []:
        try:  # one bad entry (wrong severity, a regex that does not compile) must not discard the others
            sev = str(raw.get("severity", "")).upper()
            if sev in VALID and raw.get("match"):
                rules.append((re.compile(str(raw["match"]), re.I), str(raw.get("service") or "").lower(), sev))
        except (re.error, AttributeError, TypeError):
            continue
    _rules_cache.update(key=key, rules=rules)
    return rules


def rule_severity(signal: dict) -> str | None:
    text, service = _text(signal), str(signal.get("service") or "").lower()
    for pattern, rule_service, severity in _load_rules():
        if (not rule_service or rule_service == service) and pattern.search(text):
            return severity
    return None


def service_severity(signal: dict) -> str | None:
    """SERVICE_SEVERITY="name=LOW,other=MEDIUM": a service's own default."""
    service = str(signal.get("service") or "").lower()
    for pair in _csv("SERVICE_SEVERITY"):
        name, _, sev = pair.partition("=")
        if name.strip().lower() == service and sev.strip().upper() in VALID:
            return sev.strip().upper()
    return None


def apply(signal: dict) -> dict:
    """Fields to merge into the signal: {"severity": ...} when a rule decides it, else {} (the log level stands)."""
    if is_critical(signal):
        return {"severity": "CRITICAL"}
    for decide in (rule_severity, service_severity, default_severity):
        severity = decide(signal)
        if severity:
            return {"severity": severity}
    return {}


def quiet_range() -> tuple[time, time] | None:
    m = _HOURS_RE.match(os.getenv("QUIET_HOURS", ""))
    if not m:
        return None
    a, b, c, d = (int(x) for x in m.groups())
    if a > 23 or c > 23 or b > 59 or d > 59:
        return None
    return time(a, b), time(c, d)


def _quiet_tz():
    name = os.getenv("QUIET_TIMEZONE", "").strip()
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - an unknown zone name falls back to server local time
        return None


def in_quiet_hours(now: datetime | None = None) -> bool:
    """Is it quiet time now? Read in QUIET_TIMEZONE (an IANA name), or the server's local time when unset."""
    window = quiet_range()
    if window is None:
        return False
    start, end = window
    tz = _quiet_tz()
    if now is None:
        now = datetime.now(tz)
    elif tz is not None and now.tzinfo is not None:
        now = now.astimezone(tz)
    t = now.time()
    return start <= t < end if start <= end else (t >= start or t < end)


def should_hold(signal: dict, now: datetime | None = None) -> bool:
    """True when this signal is not urgent enough to interrupt people right now."""
    if not in_quiet_hours(now):
        return False
    floor = RANK.get(os.getenv("QUIET_MIN_SEVERITY", "HIGH").strip().upper(), RANK["HIGH"])
    return RANK.get(str(signal.get("severity", "LOW")).upper(), 1) < floor

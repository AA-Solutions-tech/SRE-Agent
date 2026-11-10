"""Rule-based error classification (pure Python, no LLM).

Most production errors are not code bugs: a key missing from .env, a rejected credential, an API
throttling us, a bad recipient address, a host that is down. Those are recognisable from the log line
itself, so they are classified here, in code, before any LLM or coding agent is involved.

    tag(signal)      -> {"category": ..., "details": {...}} to merge into the signal, or {} (unclassified)
    classify(signal) -> the same plus the matched rule, or None

Unclassified signals keep the old path (LLM + code investigation). Every category listed here is an
operational problem: the incident agent sends an "action needed" notice, never a code-fix approval.
"""
import json
import re
from collections import Counter

CATEGORIES = ("config", "tls", "security", "credentials", "rate_limit", "data", "database", "deploy", "queue",
              "server", "external", "job_missed")
WATCH_CATEGORIES = ("rate_limit", "external")  # usually transient: alert only when they persist (see issues.py)

LABELS = {
    "config": "Missing or wrong configuration",
    "credentials": "Credentials rejected",
    "rate_limit": "Rate limit or quota",
    "data": "Bad data or recipient",
    "tls": "TLS certificate problem",
    "security": "Possible attack or abuse",
    "database": "Database problem",
    "deploy": "Deployment or dependency problem",
    "queue": "Queue or worker problem",
    "server": "Server or infrastructure",
    "external": "External service failing",
    "job_missed": "Scheduled job problem",
}

_VAR = r"(?P<var>[A-Z][A-Z0-9]*_[A-Z0-9_]*[A-Z0-9])"
_WORD_BEFORE = r"(?i:missing|undefined|unset|not set|not configured|not defined|empty|required)"
_WORD_AFTER = (r"(?i:is missing|is undefined|is not set|is not defined|is not configured|is required|"
               r"must be set|is empty|was not found|not found)")
_HTTP = r"(?i:status(?: code)?|http|code)\W{0,3}"

# Order matters: the first category with a matching pattern wins (most specific / most actionable first).
RULES: list[tuple[str, list[re.Pattern]]] = [
    ("config", [
        re.compile(_WORD_BEFORE + r"\b[^\n]{0,40}?\b" + _VAR + r"\b"),
        re.compile(r"\b" + _VAR + r"\b[^\n]{0,20}?" + _WORD_AFTER),
        re.compile(r"KeyError:\s*['\"]" + _VAR + r"['\"]"),
        re.compile(r"(?i)\b(?:no|missing|empty|undefined)\s+(?:\w+\s+)?(?:api[ _-]?key|secret|token|"
                   r"credentials?|env(?:ironment)? var(?:iable)?s?)\b"),
    ]),
    ("tls", [
        re.compile(r"(?i)certificate has expired|CERT_HAS_EXPIRED|unable to verify the (?:first|leaf) certificate|"
                   r"UNABLE_TO_VERIFY_LEAF_SIGNATURE|self[- ]signed certificate|CERTIFICATE_VERIFY_FAILED|"
                   r"certificate (?:is )?not yet valid|hostname/ip does not match|ERR_TLS|ssl handshake failed|"
                   r"tlsv1 alert|ERR_CERT_"),
    ]),
    ("security", [  # someone failing to get INTO this app (our own credentials being rejected is "credentials")
        re.compile(r"(?i)failed (?:login|password)|invalid user\b|too many (?:failed|invalid) (?:login|attempts)|"
                   r"brute[- ]?force|account (?:is )?locked|sql injection|path traversal|suspicious (?:request|activity)|"
                   r"blocked (?:ip|request)|port scan"),
    ]),
    ("credentials", [
        re.compile(r"(?i)unauthori[sz]ed|forbidden|invalid[_ ](?:api[_ ]?key|key|token|credentials?|password|"
                   r"client|grant)|incorrect (?:api key|password)|authentication (?:failed|error|required)|"
                   r"auth(?:entication)? failure|access denied|expired[_ ]token|token (?:has )?expired|"
                   r"expired (?:api )?key|revoked|bad credentials|\bEAUTH\b|\b535\b[ -]|invalid login|"
                   r"login failed|username and password not accepted"),
        re.compile(_HTTP + r"(?:401|403)\b"),
    ]),
    ("rate_limit", [
        re.compile(r"(?i)rate[ _-]?limit|too many requests|quota (?:exceeded|exhausted)|"
                   r"exceeded (?:your |the )?(?:quota|rate)|throttl|resource_exhausted|"
                   r"requests per (?:minute|second|day)"),
        re.compile(_HTTP + r"429\b"),
    ]),
    ("data", [  # a rejected recipient is a data problem: the job itself worked
        re.compile(r"(?i)\b55[0-4][ -]\d\.\d\.\d|user unknown|unknown user|no such user|"
                   r"mailbox (?:unavailable|not found|full|does not exist)|recipient(?: address)? rejected|"
                   r"invalid (?:recipient|e-?mail|address)|undeliverable|address rejected"),
    ]),
    ("database", [
        re.compile(r"(?i)deadlock|too many connections|lock wait timeout|relation \"[^\"]+\" does not exist|"
                   r"no such table|table '[^']+' doesn't exist|pending migrations?|migration (?:failed|required)|"
                   r"MongoServerSelectionError|server selection timed out|PrismaClientInitializationError|"
                   r"could not connect to server|remaining connection slots|database is locked|"
                   r"connection pool (?:timeout|exhausted)|SequelizeConnection\w*Error"),
    ]),
    ("deploy", [  # the code or its environment is wrong at this deploy: missing module, wrong permissions/runtime
        re.compile(r"Cannot find module ['\"](?P<var>[^'\"]+)['\"]"),
        re.compile(r"No module named ['\"](?P<var>[\w.]+)['\"]"),
        re.compile(r"\b(?:ENOENT|EACCES)\b|(?i:modulenotfounderror|importerror|cannot import name|"
                   r"no such file or directory|permission denied|unsupported engine|exec format error|"
                   r"command not found|glibc_\d)"),
    ]),
    ("queue", [
        re.compile(r"(?i)queue (?:is )?(?:full|backlog\w*)|dead[- ]letter|\bDLQ\b|worker (?:died|crashed|stalled|"
                   r"timed out)|(?:job|task) stalled|stalled (?:job|task)|consumer lag|messages? (?:are )?piling"),
    ]),
    ("server", [
        re.compile(r"\b(?:ECONNREFUSED|ECONNRESET|ETIMEDOUT|EHOSTUNREACH|ENOSPC|EMFILE)\b|"
                   r"(?i:connection refused|connection timed out|no space left|out of memory|"
                   r"cannot allocate memory|disk (?:is )?full|too many open files|socket hang up|"
                   r"read-only file system|pool (?:is )?exhausted)|\bOOM\b"),
    ]),
    ("external", [
        re.compile(r"\b(?:ENOTFOUND|ESOCKETTIMEDOUT)\b|(?i:getaddrinfo|fetch failed|bad gateway|"
                   r"service unavailable|gateway time-?out|upstream (?:is )?(?:down|unavailable|error)|"
                   r"temporarily unavailable)|" + _HTTP + r"50[234]\b"),
    ]),
]

_EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_HOST_RE = re.compile(r"(?P<host>\d{1,3}(?:\.\d{1,3}){3}|localhost|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+):(?P<port>\d{2,5})\b")
MAX_ENTITIES = 5


def _lines(signal: dict) -> list[str]:
    """The text of each evidence line (the message field of JSON logs), or the error when there is none."""
    out = []
    for raw in (signal.get("evidence") or [])[:20] or [signal.get("error") or ""]:
        text = str(raw)
        if text.lstrip().startswith("{"):
            try:
                obj = json.loads(text)
                text = f"{obj.get('message') or obj.get('msg') or ''} {obj.get('error') or ''}".strip() or text
            except ValueError:
                pass
        out.append(text)
    return out


def _match(text: str):
    """(category, pattern, match) of the first rule that fits this one line, else None."""
    for category, patterns in RULES:
        for pattern in patterns:
            m = pattern.search(text)
            if m:
                return category, pattern, m
    return None


def _entities(text: str) -> dict:
    emails = list(dict.fromkeys(_EMAIL_RE.findall(text)))[:MAX_ENTITIES]
    hosts = list(dict.fromkeys((m["host"], int(m["port"])) for m in _HOST_RE.finditer(text)))[:3]
    return {"emails": emails, "hosts": [list(h) for h in hosts]}


def classify(signal: dict) -> dict | None:
    """{"category", "rule", "details"} for a recognised operational error, else None."""
    preset = signal.get("category")
    if preset in CATEGORIES:  # set by the scheduler (job_missed) or an earlier pass
        return {"category": preset, "rule": "preset", "details": dict(signal.get("details") or {})}
    lines = _lines(signal)
    hits = [(line, _match(line)) for line in lines]
    votes = Counter(h[0] for _, h in hits if h)
    if not votes:
        return None
    order = [c for c, _ in RULES]  # most votes wins: a mixed batch is labelled by what most lines say
    winner = max(votes, key=lambda c: (votes[c], -order.index(c)))
    _, (category, pattern, m) = next((ln, h) for ln, h in hits if h and h[0] == winner)
    details = {**_entities("\n".join(lines)), "matched": m.group(0).strip()[:80],
               "lines": len(signal.get("evidence") or []), "mixed": len(votes) > 1}
    if m.groupdict().get("var"):
        details["variable"] = m["var"]
    return {"category": category, "rule": pattern.pattern[:60], "details": details}


def tag(signal: dict) -> dict:
    """Fields to merge into the signal record ({} when the error is not a known operational kind)."""
    found = classify(signal)
    return {"category": found["category"], "details": found["details"]} if found else {}

"""Known noise that must never become a card: bot probes, expected rejections, a flaky health check.

Lines that match an ignore pattern are COUNTED (so you can see how much noise there is and spot a change) but are
never turned into a signal, an issue or a notification. Patterns are case-insensitive regexes searched in the log
message and the raw line.

    IGNORE_PATTERNS   one regex per line (or separated by ;;) in the environment
    ignore file       IGNORE_FILE, default <SRE_HOME>/ignore.json
                      {"ignore": [{"pattern": "wp-login\\.php", "service": "optional", "note": "bot probe"}]}
    "Ignore similar"  the button on an incident card adds an entry to the ignore file for that error

The digest (LOW_DIGEST_HOURS) lists what was ignored, so a rule that hides something new does not go unnoticed.
"""
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sre_agent.core.config import settings
from sre_agent.core.db import create_item, list_items, update_item
from sre_agent.core.sre_log import get_logger, step

logger = get_logger("ignorelist")
KIND = "ignored_count"
MIN_PATTERN_LEN = 5
GENERIC = {"error", "failed", "exception", "unknown", "warning", "fatal", "timeout"}
_lock = threading.RLock()
_cache: dict = {"key": None, "rules": []}


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern
    text: str
    service: str
    source: str  # env | file


def file_path() -> Path:
    return Path(os.getenv("IGNORE_FILE", "").strip() or settings.data_dir / "ignore.json")


def _env_patterns() -> list[str]:
    raw = os.getenv("IGNORE_PATTERNS", "")
    return [p.strip() for chunk in raw.splitlines() for p in chunk.split(";;") if p.strip()]


def _read_file() -> list[dict]:
    try:
        data = json.loads(file_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("ignore", []) if isinstance(data, dict) else data
    return [e for e in entries if isinstance(e, dict) and e.get("pattern")] if isinstance(entries, list) else []


def rules() -> list[Rule]:
    """Compiled rules (env + file), rebuilt when either changes. A pattern that does not compile is skipped."""
    path = file_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    key = (os.getenv("IGNORE_PATTERNS", ""), str(path), mtime)
    if _cache["key"] == key:
        return _cache["rules"]
    built: list[Rule] = []
    for text, service, source in [(p, "", "env") for p in _env_patterns()] + \
            [(str(e["pattern"]), str(e.get("service") or "").lower(), "file") for e in _read_file()]:
        try:
            built.append(Rule(re.compile(text, re.I), text, service, source))
        except re.error as e:
            step(logger, "ignorelist.01.load", "ignore pattern skipped", level="error", pattern=text[:80], error=str(e)[:100])
    _cache.update(key=key, rules=built)
    return built


def matches(event: dict) -> Rule | None:
    text = f"{event.get('message', '')}\n{event.get('raw', '')}"
    service = str(event.get("service") or "").lower()
    for rule in rules():
        if (not rule.service or rule.service == service) and rule.pattern.search(text):
            return rule
    return None


def split(events: list[dict]) -> tuple[list[dict], list[tuple[dict, Rule]]]:
    """(events to keep, [(event, rule)] that are ignored)."""
    kept, dropped = [], []
    for ev in events:
        rule = matches(ev)
        (dropped.append((ev, rule)) if rule else kept.append(ev))
    return kept, dropped


def record(dropped: list[tuple[dict, Rule]]) -> None:
    """Count ignored lines per pattern (kept in the database, so the digest and /stats can show them)."""
    if not dropped:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        existing = {i["data"].get("pattern"): i for i in list_items(kind=KIND)}
        per_rule: dict[str, int] = {}
        for _, rule in dropped:
            per_rule[rule.text] = per_rule.get(rule.text, 0) + 1
        for text, n in per_rule.items():
            item = existing.get(text)
            if item:
                update_item(item["id"], count=item["data"].get("count", 0) + n, last_seen=now)
            else:
                create_item(KIND, {"pattern": text, "count": n, "digest_count": 0, "first_seen": now, "last_seen": now})


def digest_rows() -> list[dict]:
    """[{pattern, new, count}] for patterns that ignored lines since the last digest (each is marked as reported)."""
    out = []
    for item in list_items(kind=KIND):
        d = item["data"]
        new = d.get("count", 0) - d.get("digest_count", 0)
        if new > 0:
            out.append({"pattern": d.get("pattern"), "new": new, "count": d.get("count", 0)})
            update_item(item["id"], digest_count=d.get("count", 0))
    return sorted(out, key=lambda r: -r["new"])


def pattern_for(issue: dict) -> str:
    """A regex that matches this error's lines, from the issue's error name. "" when the name is too generic to
    ignore safely (ignoring "error" would hide everything)."""
    name = str(issue.get("error") or "").strip()
    if len(name) < MIN_PATTERN_LEN or name.lower() in GENERIC:
        return ""
    return re.escape(name)


def add(pattern: str, service: str = "", note: str = "") -> bool:
    """Append a rule to the ignore file. False when it already exists. Raises ValueError for a pattern that is empty,
    too short, does not compile, or would match everything."""
    pattern = (pattern or "").strip()
    if len(pattern) < MIN_PATTERN_LEN:
        raise ValueError("pattern is too short to ignore safely")
    try:
        compiled = re.compile(pattern, re.I)
    except re.error as e:
        raise ValueError(f"pattern does not compile: {e}") from None
    if compiled.search("") or all(compiled.search(s) for s in ("a", "error", "ok 200", "x" * 40)):
        raise ValueError("pattern would match every line")
    with _lock:
        entries = _read_file()
        service = (service or "").strip().lower()
        if any(e.get("pattern") == pattern and str(e.get("service") or "").lower() == service for e in entries):
            return False
        entries.append({"pattern": pattern, "service": service or None, "note": note[:200],
                        "added": datetime.now(timezone.utc).isoformat()})
        path = file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ignore": entries}, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    step(logger, "ignorelist.02.add", "ignore rule added", pattern=pattern[:80], service=service or "any")
    return True

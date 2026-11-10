"""Slack Block Kit layout for the SRE cards.

Every message kind gets its own emoji + short header, so a new error, an investigation, a routing note
and a reminder are told apart at a glance. The body (mrkdwn with *UPPERCASE* headings) is split into one
block per heading; "ON APPROVE" becomes small print under the buttons. Titles of unknown kinds return
None so notify() keeps its plain header + body layout.
"""
import re

KINDS = {  # title prefix (lower case) -> (emoji, header label)
    "signal detected": ("🚨", "New error detected"),
    "investigate": ("🔍", "Investigation"),
    "action needed": ("🧰", "Action needed"),
    "recovered": ("✅", "Recovered"),
    "fix failed": ("❌", "Fix failed"),
    "digest": ("🗒️", "Digest"),
    "sre supervisor": ("🧭", "Routing"),
    "reminder": ("⏰", "Still failing"),
    "fix ready": ("🛠️", "Fix ready for review"),
    "fix shipped": ("🚀", "Fix shipped"),
    "coding done": ("✅", "Coding finished"),
    "coding failed": ("❌", "Coding failed"),
    "revision limit reached": ("⚠️", "Revision limit reached"),
    "plan revision failed": ("⚠️", "Plan revision failed"),
}
SEVERITY_EMOJI = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}
HEADING_EMOJI = {
    "SUMMARY": "📝", "IMPACT": "💥", "ROOT CAUSE": "🧬", "WHERE IN THE CODE": "📍", "CODE EVIDENCE": "🔎",
    "LOG EVIDENCE": "📜", "RECOMMENDED FIX": "🛠️", "SCOPE OF CHANGE": "📐", "TESTS TO PROVE IT": "🧪",
    "NEXT STEPS": "👉", "WHAT WAS CHECKED": "🔬", "WHAT TO DO": "🛠️",
    "AI ANALYSIS UNAVAILABLE": "⚠️", "INCIDENT": "📌", "ERROR": "❗", "DETAILS": "📌", "PATTERN": "🔁",
}
SECTION_LIMIT = 2900
_HEAD = re.compile(r"^\*([A-Z][A-Z0-9 '()/&-]+?)\*(?:\s+(.*))?$")
_TITLE = re.compile(r"^(?P<service>[\w.\-/]+) (?P<sev>CRITICAL|HIGH|MEDIUM|LOW)\b\s*(?P<error>.*?)"
                    r"(?: \[trace (?P<trace>\w+)\])?$")


def _clip(text: str, limit: int = SECTION_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _clip(text)}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _clip(text)}]}


def _split(body: str) -> tuple[str, list[tuple[str, str, str]]]:
    """(text before the first heading, [(HEADING, same-line note, text)])."""
    pre, sections, cur = [], [], None
    for line in body.splitlines():
        m = _HEAD.match(line.strip())
        if m:
            cur = (m.group(1), (m.group(2) or "").strip(), [])
            sections.append(cur)
        elif cur:
            cur[2].append(line)
        else:
            pre.append(line)
    return "\n".join(pre).strip(), [(h, sub, "\n".join(t).strip()) for h, sub, t in sections]


def _kind(title: str) -> tuple[str, str, str] | None:
    low = title.lower()
    for key, (emoji, label) in KINDS.items():
        if low.startswith(key):
            return emoji, label, title[len(key):].lstrip(": ").strip()
    return None


def _headline(rest: str) -> list[dict]:
    """Service / severity / trace as a context line, and the error as a bold line."""
    m = _TITLE.match(rest)
    if not m:
        return [_section(f"*{rest}*")] if rest else []
    bits = [f"*{m['service']}*", f"{SEVERITY_EMOJI.get(m['sev'], '⚪')} {m['sev']}"]
    if m["trace"]:
        bits.append(f"trace `{m['trace']}`")
    out = [_context("  ·  ".join(bits))]
    if m["error"]:
        out.append(_section(f"*{m['error']}*"))
    return out


def layout(title: str, body: str) -> tuple[list[dict], list[dict]] | None:
    """(blocks before the buttons, blocks after them), or None when `title` is not a known card kind."""
    kind = _kind(title)
    if kind is None:
        return None
    emoji, label, rest = kind
    if title.lower().startswith("sre supervisor"):  # routing note: one quiet line, no header
        return [_section(f"{emoji} *{label}*  ·  {rest}\n{body}")], []
    before = [{"type": "header", "text": {"type": "plain_text", "text": f"{emoji} {label}"[:150]}}]
    before += _headline(rest)
    pre, sections = _split(body)
    small_print: list[dict] = []
    content: list[dict] = [_section(pre)] if pre else []
    for heading, note, text in sections:
        if heading == "ON APPROVE":
            small_print = [_context(f"*What the buttons do*\n{text}")]
            continue
        icon = HEADING_EMOJI.get(heading, "▪️")
        content.append(_section(f"{icon} *{heading.capitalize()}*" + (f"  _{note}_" if note else "")
                                + (f"\n{text}" if text else "")))
    if content:
        before.append({"type": "divider"})
    before += content[:40]
    return before + [{"type": "divider"}], small_print

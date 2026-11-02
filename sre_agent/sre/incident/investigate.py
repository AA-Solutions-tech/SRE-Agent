"""Bounded, tool-using investigation for the incident agent (read-only on REPO_DIR).

The model works like a developer: it greps, reads files, finds who uses a symbol, checks git
history, then answers in JSON. Everything it can do is read-only and budgeted
(INCIDENT_MAX_STEPS tool rounds). Afterwards plain code -- not the model -- verifies the answer:

  * every citation (path, line, quote) must really appear in the file near that line;
  * affected_files must be files the model actually read;
  * git history / blame for those files is attached from git itself;
  * confidence is capped when citations are missing or fail.
"""
import json
import os
import re
import statistics
from datetime import datetime
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from sre_agent.core.git_tools import BLOCKED_PARTS, _git, safe_path
from sre_agent.core.llm import get_llm, text_of
from sre_agent.core.sre_log import get_logger, step

logger = get_logger("incident")

MAX_STEPS = int(os.getenv("INCIDENT_MAX_STEPS", "10"))
MAX_READ_LINES = 150
SKIP_EXT = (".md", ".json", ".lock", ".html", ".txt", ".log", ".svg", ".csv", ".map")
SKIP_PARTS = ("test", "__tests__", ".spec.", "mock", "fixtures", "node_modules")
CONFIG_EXT = (".md", ".json", ".yml", ".yaml", ".toml", ".env.example", ".conf", ".ini")
CITATION_WINDOW = 5
NL = chr(10)

SYSTEM = """You are a senior SRE investigating a production error in a code repository.
You have read-only tools. Work like a developer: locate the failing code, read the whole
function, find where its inputs come from and who calls it, and check recent history. Use at
most {steps} tool rounds, then answer. Never guess file contents: if you did not read it, do not
cite it. Do not invent files, hosts, tokens or history.

Efficiency rules: start by reading the function around the logged line; never repeat a search you
already ran; stop investigating as soon as you can name the root cause and cite its lines. Searching
more after that is wasted budget.

Think about at least TWO possible explanations and say why you rejected the others.

When done, reply with ONE JSON object and nothing else, with exactly these keys:
{{
 "summary": "one sentence a manager can read",
 "impact": "what breaks for users/features and how widespread (use the pattern data)",
 "category": one of {categories},
 "fix_type": "code" if a source change fixes it, else "ops" (credentials, config, infra, data),
 "root_cause": "2-4 sentences: the chain from trigger to the logged error, naming functions and lines",
 "remediation": "concrete steps; for fix_type=code the exact change (function, condition, guard)",
 "affected_files": ["files that must be EDITED to fix it (paths you READ with read_file); [] for ops fixes"],
 "context_files": ["other files you read only to understand the problem, not to edit"],
 "estimated_lines": "rough number of lines the fix adds/changes/removes in total, as an integer (0 for ops)",
 "citations": [{{"path": str, "line": int, "quote": "exact text copied from that line, max 120 chars"}}],
 "alternatives": [{{"hypothesis": str, "why_rejected": str}}],
 "evidence": ["short facts supporting the root cause"],
 "confidence": number 0..1,
 "risk": one of {risks},
 "required_tests": ["specific test cases that prove the fix"],
 "next_steps": ["what a human should do besides approving"]
}}"""


def _skip(path: str) -> bool:
    low = path.lower()
    return bool(BLOCKED_PARTS & set(Path(path).parts)) or low.endswith(SKIP_EXT) or any(s in low for s in SKIP_PARTS)


def _hits(out: str, keep, limit: int) -> list[str]:
    rows = []
    for line in out.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3 or not parts[1].isdigit() or not keep(parts[0]):
            continue
        rows.append(f"{parts[0]}:{parts[1]}: {parts[2].strip()[:160]}")
        if len(rows) >= limit:
            break
    return rows


def make_tools(repo: Path, read: set[str]) -> list:
    """Read-only tools bound to `repo`. `read` collects the files the model actually opened."""

    @tool
    def grep_code(term: str) -> str:
        """Fixed-string search in tracked SOURCE files (not tests/docs). Returns up to 20 'path:line: text' hits."""
        rows = _hits(_git(repo, "grep", "-n", "-I", "-F", "-e", term, check=False), lambda p: not _skip(p), 20)
        return "\n".join(rows) or "no matches"

    @tool
    def read_file(path: str, start_line: int = 1, end_line: int = 120, line_start: int | None = None,
                  line_end: int | None = None) -> str:
        """Read numbered lines of a repo file (max 150 lines per call). Use it to read whole functions.
        Arguments: path, start_line, end_line (line_start/line_end are accepted as aliases)."""
        p = safe_path(repo, path)
        if not p.is_file():
            return f"not a file: {path}"
        lines = p.read_bytes().decode("utf-8", errors="replace").splitlines()
        start = max(1, int(line_start if line_start is not None else start_line))
        end = min(len(lines), int(line_end if line_end is not None else end_line), start + MAX_READ_LINES - 1)
        read.add(p.relative_to(repo.resolve()).as_posix())
        body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
        return f"{path} lines {start}-{end} of {len(lines)}\n{body}"

    @tool
    def find_usages(symbol: str) -> str:
        """Find where a function/variable name is used (whole-word search in source files). Up to 25 hits."""
        rows = _hits(_git(repo, "grep", "-n", "-I", "-w", "-F", "-e", symbol, check=False), lambda p: not _skip(p), 25)
        return "\n".join(rows) or "no usages"

    @tool
    def git_history(path: str, count: int = 5) -> str:
        """Recent commits that touched a file: '<hash> <date> <author>: <subject>'."""
        safe_path(repo, path)
        out = _git(repo, "log", f"-n{min(int(count), 10)}", "--date=short", "--format=%h %ad %an: %s", "--", path,
                   check=False)
        return out or "no history"

    @tool
    def search_config(term: str) -> str:
        """Search config and docs (md/json/yml/env.example) for a name. Up to 10 hits."""
        keep = lambda p: p.lower().endswith(CONFIG_EXT) and "node_modules" not in p and "lock" not in p.lower()  # noqa: E731
        rows = _hits(_git(repo, "grep", "-n", "-I", "-F", "-e", term, check=False), keep, 10)
        return "\n".join(rows) or "no matches in config/docs"

    return [grep_code, read_file, find_usages, git_history, search_config]


def _notes(msgs) -> str:
    """Plain-text record of every tool call and result (for a final answer without tool bindings)."""
    out, calls = [], {}
    for m in msgs:
        for c in getattr(m, "tool_calls", None) or []:
            calls[c["id"]] = f"{c['name']}({c['args']})"
        if isinstance(m, ToolMessage):
            out.append("### " + calls.get(m.tool_call_id, "tool") + NL + str(m.content)[:3500])
    return (NL + NL).join(out)


def run_loop(prompt: str, tools: list, categories, risks) -> str:
    """Tool-calling loop (bounded). Returns the model's final text (expected: one JSON object).

    The final answer is requested WITHOUT tool bindings, from a plain-text transcript of what the
    tools returned: some providers fail to parse long JSON when tools are still bound.
    """
    llm = get_llm(temperature=0.0)
    bound = llm.bind_tools(tools)
    by_name = {t.name: t for t in tools}
    system = SystemMessage(SYSTEM.format(steps=MAX_STEPS, categories=list(categories), risks=list(risks)))
    msgs = [system, HumanMessage(prompt + NL + NL + "Use the tools to investigate. When you have enough "
                                 "evidence, reply with the single word DONE.")]
    rounds = 0
    done_calls: set = set()
    for i in range(1, MAX_STEPS + 1):
        try:
            ai = bound.invoke(msgs)
        except Exception as e:  # provider could not parse the model output -> answer from what we have
            step(logger, "incident.02c.investigate", "tool round failed, answering from notes so far",
                 level="warning", round=i, error=f"{type(e).__name__}: {e}"[:160])
            break
        msgs.append(ai)
        calls = getattr(ai, "tool_calls", None) or []
        if not calls:
            break  # model says it is done
        rounds = i
        for c in calls:
            tl = by_name.get(c["name"])
            key = (c["name"], json.dumps(c["args"], sort_keys=True, default=str))
            if key in done_calls:  # same call again: do not burn a round on it
                result = "DUPLICATE CALL - you already have this result above. Try a different search or answer."
            else:
                done_calls.add(key)
                try:
                    result = tl.invoke(c["args"]) if tl else f"unknown tool {c['name']}"
                except Exception as e:  # a bad path/arg must not kill the investigation
                    result = f"tool error: {type(e).__name__}: {e}"
            step(logger, "incident.02c.investigate", "tool call", round=i, tool=c["name"],
                 tool_args=str(c["args"])[:120], result_chars=len(str(result)))
            msgs.append(ToolMessage(content=str(result)[:6000], tool_call_id=c["id"]))
        left = MAX_STEPS - i
        if 0 < left <= 2:  # nudge the model to converge before the budget runs out
            msgs.append(HumanMessage(
                f"{left} tool round(s) left. If you can already name the root cause and cite its lines, "
                "reply DONE now; otherwise make your last searches count."))
    else:
        step(logger, "incident.02c.investigate", "step budget used", level="warning", rounds=MAX_STEPS)
    step(logger, "incident.02c.investigate", "requesting final answer", rounds=rounds)
    final = HumanMessage(NL.join([prompt, "", "INVESTIGATION NOTES (tool results):", _notes(msgs), "",
                                  "Now reply with the final JSON object only."]))
    try:  # providers that support it are forced to emit a JSON object
        reply = text_of(llm.bind(response_format={"type": "json_object"}).invoke([system, final]))
    except Exception:
        reply = text_of(llm.invoke([system, final]))
    if not re.search(r"\{.*\}", reply, re.DOTALL):  # no JSON came back: ask once more, explicitly
        step(logger, "incident.02c.investigate", "final answer had no JSON, retrying once", level="warning",
             reply_chars=len(reply))
        reply = text_of(llm.invoke([system, final, HumanMessage(
            "Your previous reply contained no JSON object. Output ONLY the JSON object now, starting with { "
            "and ending with }, no commentary.")]))
    return reply


# ---------------- verification (plain code, no LLM) ----------------
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def verify_citations(repo: Path, citations) -> tuple[list[dict], int]:
    """(verified, failed_count). A citation is verified if its quote occurs within +-5 lines of `line`."""
    verified, failed = [], 0
    for c in citations if isinstance(citations, list) else []:
        try:
            path, line, quote = str(c["path"]), int(c["line"]), _norm(str(c["quote"]))
            lines = safe_path(repo, path).read_bytes().decode("utf-8", errors="replace").splitlines()
            window = _norm(" ".join(lines[max(0, line - 1 - CITATION_WINDOW): line + CITATION_WINDOW]))
            if quote and quote in window:
                verified.append({"path": Path(path).as_posix(), "line": line, "quote": quote[:120]})
                continue
        except (KeyError, ValueError, TypeError, OSError):
            pass
        failed += 1
    return verified, failed


def history_for(repo: Path, files: list[str], refs: dict[str, int]) -> list[str]:
    """Recent commits per affected file plus who last changed the cited line (from git itself)."""
    out = []
    for f in files[:3]:
        log = _git(repo, "log", "-n3", "--date=short", "--format=%h %ad %an: %s", "--", f, check=False)
        for row in log.splitlines():
            out.append(f"{f}: {row}")
        line = refs.get(f)
        if line:
            blame = _git(repo, "blame", "-L", f"{line},{line}", "-w", "--date=short", "--", f, check=False)
            m = re.match(r"\^?(\w+)\s+\((.+?)\s+(\d{4}-\d{2}-\d{2})\s+\d+\)", blame)
            if m:
                out.append(f"{f}:{line} last changed in {m.group(1)} by {m.group(2)} on {m.group(3)}")
    return out


def _parse_ts(value) -> datetime | None:
    s = str(value or "").strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S:%f",):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        return None


def pattern_text(signal: dict, related: list[dict]) -> str:
    """Occurrence pattern from stored signals: count, first/last, typical gap between occurrences."""
    stamps = sorted(t for t in (_parse_ts(r.get("ts")) for r in related) if t)
    now = _parse_ts(signal.get("ts"))
    if now:
        stamps.append(now)
        stamps.sort()
    total = len(related) + 1
    if len(stamps) < 2:
        return f"{total} signal(s) for this service; not enough timestamps to compute a pattern"
    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    gap = statistics.median(gaps)
    unit = f"{gap / 60:.1f} min" if gap >= 90 else f"{gap:.0f} s"
    span = (stamps[-1] - stamps[0]).total_seconds() / 60
    return (f"{total} signals over {span:.0f} min ({stamps[0]:%Y-%m-%d %H:%M} to {stamps[-1]:%H:%M}); "
            f"typical gap between occurrences {unit}")


def calibrate(conf: float, fix_type: str, verified: list, failed: int, files: list) -> float:
    """Cap model confidence by evidence the code could verify."""
    if fix_type == "code" and (not files or not verified):
        conf = min(conf, 0.3)
    if failed:
        conf = min(conf, 0.5)
    return conf

"""Pure RCA helpers (no I/O, no LLM calls here): prompt building,
offline heuristic fallback, and the human summary card renderer.

Boundary rule: machines exchange the signal JSON; humans get the
rendered markdown summary. Raw JSON never reaches Slack.
"""
import json
from sre_agent.sre import issues, revision

EVIDENCE_SHOWN = 5

RCA_PROMPT = """You are an SRE investigator. Root-cause the signal below using
ONLY the evidence given. Do not invent services, hosts, metrics, or history
that is not in the data. Reply in 3 short sections: Likely cause / Evidence /
Suggested fix (one line, for the coding agent).

SIGNAL:
{signal}

RELATED PAST SIGNALS (same service):
{related}"""


def build_rca_prompt(signal: dict, related: list[dict]) -> str:
    return RCA_PROMPT.format(
        signal=json.dumps(signal, indent=1)[:6000],
        related=json.dumps(related, indent=1)[:4000] or "none",
    )


def heuristic_rca(signal: dict, occurrences: int) -> str:
    """Deterministic RCA when no LLM is available (offline-safe)."""
    return (
        f"Likely cause: {signal.get('error', 'unknown')} on "
        f"{signal.get('service', 'unknown')} "
        f"({signal.get('severity', '?')} severity).\n"
        f"Evidence: {len(signal.get('evidence', []))} log line(s); "
        f"seen {occurrences} time(s) on this service.\n"
        f"Suggested fix: inspect {signal.get('service', 'unknown')} around "
        f"{signal.get('ts', '?')} and address {signal.get('error', 'unknown')}."
    )


def build_card(signal: dict, rca: str, occurrences: int) -> tuple[str, str]:
    """Render the human summary (title, markdown body) for Slack."""
    service = signal.get("service", "?")
    severity = signal.get("severity", "?")
    error = signal.get("error", "?")
    evidence = signal.get("evidence", []) or []
    shown = "\n".join(f"`{line[:160]}`" for line in evidence[:EVIDENCE_SHOWN])
    more = f"\n_+{len(evidence) - EVIDENCE_SHOWN} more lines_" if len(evidence) > EVIDENCE_SHOWN else ""
    title = f"investigate: {service} {severity} {error}"
    body = (
        f"*Service:* {service}   *Severity:* {severity}   *Error:* {error}\n"
        f"*Occurrences on this service:* {occurrences}\n\n"
        f"*Root cause analysis*\n{rca}\n\n"
        f"*Evidence ({len(evidence)} lines)*\n{shown}{more}"
    )
    return title, body


FIX_LABEL = {"code": "code change (coding agent can prepare it)", "ops": "operational / config action (no code change)"}


def build_analysis_card(signal: dict, analysis: dict, occurrences: int, signal_id: str = "",
                        issue: dict | None = None, version: int = 1, changes: list[str] | None = None,
                        instruction: str = "", revisions_left: int = 0) -> tuple[str, str]:
    """Fixed-layout incident card rendered from the structured analysis (never free LLM text)."""
    service = signal.get("service", "?")
    severity = signal.get("severity", "?")
    error = signal.get("error", "?")
    evidence = analysis.get("evidence") or signal.get("evidence", []) or []
    refs = analysis.get("code_refs") or []
    code = "\n".join(f"• `{r['path']}:{r['line']}`" for r in refs) or "• not located in the repository"
    n_edit = len(analysis.get("affected_files") or [])
    n_ctx = len(analysis.get("context_files") or [])
    lines = int(analysis.get("estimated_lines") or 0)
    if analysis.get("fix_type") == "code" and n_edit:
        scope = (f"{n_edit} file(s) will be edited, about {lines or '?'} line(s) in total:\n"
                 + "\n".join(f"• `{f}`" for f in analysis["affected_files"]))
    else:
        scope = "No files will be changed by this fix."
    if n_ctx:
        scope += f"\n{n_ctx} other file(s) were read for context only: " + ", ".join(
            f"`{f}`" for f in analysis["context_files"])
    bullets = lambda items: "\n".join(f"• {i}" for i in items) or "• none"  # noqa: E731
    conf = f"{round(float(analysis.get('confidence', 0)) * 100)}%"
    fix_type = analysis.get("fix_type", "ops")
    approve_text = ("The coding agent will prepare a fix on a new branch in an isolated worktree and run the "
                    "checks. Nothing is pushed or deployed." if fix_type == "code" else
                    "No code change is recommended, so approving only records your decision; "
                    "follow the next steps above.")
    hours = issues.remind_interval().total_seconds() / 3600
    quiet = issues.quiet_window().total_seconds() / 60
    on_approve = (
        f"• Approve: {approve_text}\n"
        f"• Reject: no action; the issue is snoozed for {issues.snooze_window().total_seconds() / 3600:g} h.\n"
        f"• I'll fix this: you take it yourself. The coding agent does not run, and you are reminded every "
        f"{hours:g} h until the error has stopped for {quiet:g} min."
        + (chr(10) + f"• Request changes: opens a form to ask for a new plan version ({revisions_left} revision(s) left)."
           + (" You can also just reply to this email; optional first-column lines: deny: path, only: folder, "
              "also: file, test: description, ops only." if revision.reply_enabled() else "")
           if revisions_left > 0 and analysis.get("root_cause") else "")
    )
    nl = chr(10)
    version_line = f"Plan version {version} (revised, see what changed below)" + nl if version > 1 else ""
    changed = ""
    if version > 1:
        quote = instruction.strip()[:400] + ("..." if len(instruction.strip()) > 400 else "")
        changed = (f"*WHAT CHANGED SINCE VERSION {version - 1}*{nl}"
                   + (f"Reviewer asked: \"{quote}\"{nl}" if quote else "")
                   + nl.join(f"• {c}" for c in (changes or ["no visible change"])) + nl + nl)
    seen = f"{issues.describe(issue['data'])}\n" if issue else ""
    verified = analysis.get("verified_citations") or []
    unverified = int(analysis.get("unverified_citations") or 0)
    cited = "\n".join(f"• `{c['path']}:{c['line']}`  {c['quote']}" for c in verified) or "• none verified"
    if unverified:
        cited += f"\n• {unverified} citation(s) from the model could NOT be found in the code and were dropped"
    alts = [f"{a['hypothesis']} — rejected: {a['why_rejected']}" for a in analysis.get("alternatives") or []]
    pattern = f"*PATTERN*\n{analysis['pattern']}\n\n" if analysis.get("pattern") else ""
    history = analysis.get("history") or []
    limited = ("_Limited analysis: the step-by-step code investigation did not finish, so this answer was not "
               "cross-checked against the repository. Confidence is capped._\n\n" if analysis.get("limited") else "")
    title = f"investigate: {service} {severity} {error}"
    body = (
        f"{limited}"
        f"{version_line}"
        f"*INCIDENT*  {service} · {severity} · {error}\n"
        f"{seen}"
        f"Signal `{signal_id or '?'}` · {occurrences} occurrence(s) of this error · "
        f"{len(signal.get('evidence', []) or [])} log line(s) in this signal · last seen {signal.get('ts', '?')}\n\n"
        f"*SUMMARY*\n{analysis.get('summary') or error}\n\n"
        f"*IMPACT*\n{analysis.get('impact') or 'unknown'}\n\n"
        f"{changed}"
        f"{pattern}"
        f"*ROOT CAUSE*  (category: {analysis.get('category', 'unknown')} · confidence {conf})\n"
        f"{analysis.get('root_cause', '')}\n\n"
        f"*WHERE IN THE CODE*\n{code}\n\n"
        f"*CODE EVIDENCE (checked against the repository)*\n{cited}\n\n"
        + (f"*OTHER EXPLANATIONS CONSIDERED*\n{bullets(alts)}\n\n" if alts else "")
        + (f"*RECENT HISTORY OF THESE FILES*\n{bullets(history[:6])}\n\n" if history else "")
        + f"*LOG EVIDENCE*\n{bullets([str(e)[:200] for e in evidence[:6]])}\n\n"
        f"*RECOMMENDED FIX*  ({FIX_LABEL.get(fix_type, fix_type)} · risk: {analysis.get('risk', 'medium')})\n"
        f"{analysis.get('remediation', '')}\n\n"
        f"*SCOPE OF CHANGE*\n{scope}\n\n"
        f"*TESTS TO PROVE IT*\n{bullets(analysis.get('required_tests') or [])}\n\n"
        f"*NEXT STEPS*\n{bullets(analysis.get('next_steps') or [])}\n\n"
        f"*ON APPROVE*\n{on_approve}"
    )
    return title, body

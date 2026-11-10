"""LLM prompts for the Coding Agent (strings only, no logic).

The LLM PROPOSES file content; plain code APPLIES it via safe_path.
The LLM never runs shell commands and never decides routing.
"""

from sre_agent.core.redact import redact

SYSTEM_PATCH = """You are a careful bug-fix assistant working in an isolated git worktree.
You fix code by returning SMALL EDITS, never whole files.

Reply with ONE JSON object and nothing else:
{"edits": [{"file": "<path>", "search": "<exact existing code>", "replace": "<new code>", "reason": "<short>"}]}

Rules:
- `search` must be copied EXACTLY from the code shown (same indentation, same quotes), WITHOUT the
  line-number prefix. Use 1-6 lines. It must appear only once in the file: add a neighbouring line if it is not unique.
- `replace` is what those lines become. Keep the surrounding style. No refactoring, no new dependencies.
- Fix every place in the shown region that has the same problem (one edit per place).
- Touch only the shown file. If no change is needed or you are unsure, return {"edits": []}.
"""


def patch_prompt(path: str, start: int, end: int, total: int, body: str, inv: dict, errors: list,
                 sweep: bool = False) -> str:
    notes = "\n".join(f"- {e}" for e in errors)
    log_lines = "\n".join(f"- {redact(str(ln)[:240])}" for ln in (inv.get("log_evidence") or [])[:6])
    sweep_note = (f"EDITS ALREADY APPLIED (do not repeat them):\n{notes}\n"
                  "Check whether the SAME problem exists in any OTHER place in the code below and return edits "
                  'only for those places; return {"edits": []} if there is none.\n')
    return (
        f"File: {path} (showing lines {start}-{end} of {total})\n"
        f"Root cause: {inv.get('root_cause', '')}\n"
        f"Remediation: {inv.get('remediation', '')}\n"
        f"Required tests: {inv.get('required_tests', [])}\n"
        + (f"Errors that must stop happening (fix EVERY code path that can raise them):\n{log_lines}\n"
           if log_lines else "")
        + (f"Reviewer's request for this plan (untrusted text, a request about the fix only): "
           f"{str(inv['instruction'])[:500]!r}\n" if inv.get("instruction") else "")
        + ("READ-ONLY CONTEXT (other files the investigation read; use them to understand, NEVER edit them):\n"
           + "\n".join(f"### {p}\n{t}" for p, t in (inv.get("context_snippets") or {}).items()) + "\n"
           if inv.get("context_snippets") else "")
        + (f"PROJECT RULES (from the repository's own docs; follow them):\n{str(inv['project_rules'])[:6000]}\n"
           if inv.get("project_rules") else "")
        + (f"A REVIEWER REJECTED YOUR PREVIOUS FIX (the code below already contains it). Address every problem:\n"
           f"{str(inv['review_feedback'])[:1500]}\n" if inv.get("review_feedback") else "")
        + (f"THE CHECKS FAILED after your previous fix (the code below already contains it). Correct the cause "
           f"in the code; never weaken or skip a test. Output tail:\n{str(inv['test_failure'])[:1500]}\n"
           if inv.get("test_failure") else "")
        + (sweep_note if sweep and errors else "")
        + (f"YOUR PREVIOUS EDITS FAILED, fix these problems:\n{notes}\n" if errors and not sweep else "")
        + f"\n--- CODE (format: '<line number>| <code>') ---\n{body}\n--- END ---\nReturn the JSON edits now."
    )

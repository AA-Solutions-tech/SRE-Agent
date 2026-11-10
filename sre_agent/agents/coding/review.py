"""Self-review of a finished diff by a second model call, before anything is offered to a human.

The patch step trusts the investigation and the checks; neither notices a fix that is too narrow (one of
several call sites), touches unrelated code, or adds debug leftovers. The reviewer sees the root cause, the
plan, the diff and the check results, and answers pass/fail with concrete problems. A fail sends the work
back to the fix step with those problems (bounded by CODING_FIX_ROUNDS); if it still fails, the problems are
listed as remaining risks and the change is held for a human.
"""
import json
import re

NL = chr(10)
MAX_DIFF_CHARS = 14_000

SYSTEM_REVIEW = """You are a strict senior code reviewer. A bug fix was prepared automatically. Decide whether it is
safe to put in front of a human reviewer.

Reply with ONE JSON object and nothing else:
{"verdict": "pass" | "fail", "problems": ["<specific problem, file and what is wrong>"]}

Fail ONLY for real problems:
- the diff does not fix the stated root cause, or fixes only one of several places with the same problem;
- it changes unrelated code, behaviour, formatting or dependencies;
- it can introduce a new bug (wrong condition, swallowed error, changed return type, missing null case);
- it leaves debug code, secrets, TODOs or a weakened/skipped test.
Style nitpicks are not problems. If the diff is a sound minimal fix, reply {"verdict": "pass", "problems": []}.
"""


def review_prompt(inv: dict, diff_text: str, tests: list[dict]) -> str:
    checks = NL.join(f"- {'pass' if t.get('passed') else 'FAIL'}: {t.get('command')}" for t in tests) or "- none ran"
    cut = diff_text[:MAX_DIFF_CHARS] + (NL + "... (diff shortened)" if len(diff_text) > MAX_DIFF_CHARS else "")
    return NL.join([
        f"Error: {inv.get('error', '')}", f"Root cause: {inv.get('root_cause', '')}",
        f"Planned remediation: {inv.get('remediation', '')}", f"Tests the plan asked for: {inv.get('required_tests', [])}",
        "", "CHECKS:", checks, "", "DIFF:", cut, "", "Reply with the JSON verdict now."])


def parse_verdict(reply: str) -> dict | None:
    """{"verdict": "pass"|"fail", "problems": [...]} or None when the reply cannot be understood."""
    m = re.search(r"\{.*\}", reply, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            verdict = str(obj.get("verdict", "")).strip().lower()
            if verdict in ("pass", "fail"):
                problems = [str(p).strip() for p in (obj.get("problems") or []) if str(p).strip()]
                return {"verdict": verdict, "problems": problems[:6]}
        except ValueError:
            pass
    head = reply.strip().upper()
    if head.startswith("PASS"):
        return {"verdict": "pass", "problems": []}
    if head.startswith("FAIL"):
        return {"verdict": "fail", "problems": [reply.strip()[4:].lstrip(": ")[:300] or "reviewer said fail"]}
    return None


def review(inv: dict, diff_text: str, tests: list[dict], ask) -> dict:
    """Ask the reviewer. Raises when the model cannot be reached or answers nonsense (the caller records a risk)."""
    verdict = parse_verdict(ask(SYSTEM_REVIEW + NL + NL + review_prompt(inv, diff_text, tests)))
    if verdict is None:
        raise ValueError("the reviewer's reply was not a verdict")
    if verdict["verdict"] == "fail" and not verdict["problems"]:
        verdict["problems"] = ["the reviewer rejected the fix without saying why"]
    return verdict

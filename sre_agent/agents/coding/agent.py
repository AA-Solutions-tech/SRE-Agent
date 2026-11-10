"""Coding Agent (Agent 3): implement an approved investigation fix in isolation.

Flow (Supervisor routes; this agent never routes):
  Supervisor --CodingInput JSON--> validate -> setup -> context -> fix -> verify -> report
  --CodingResult JSON--> Supervisor --VerifyTask--> Monitor --> Supervisor

Safety:
  * Works ONLY inside its own worktree on branch <BRANCH_PREFIX>/<incident_id>.
  * Never touches REPO_DIR directly; never commits/pushes/merges/deploys.
  * LLM PROPOSES content; plain code applies it via safe_path. No arbitrary shell.
  * Timeouts + bounded retries; honest failure (never claim success without tests).

    python run.py coding --incident-id INC1   # (signal/investigation via DB in prod;
                                              #  tests pass dicts directly)
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from sre_agent.core import git_tools as g
from sre_agent.core.config import settings
from sre_agent.core.db import create_item, get_item
from sre_agent.core.llm import get_llm, text_of
from sre_agent.core.notify import notify
from sre_agent.core.redact import redact
from sre_agent.core.sre_log import bind_trace, get_logger, step

from . import patch, review, tests_gen
from .schemas import CodingResult
from .validate import validate

KIND = "coding_result"
NL = chr(10)
MAX_CONTEXT_CHARS = 1_000_000  # files larger than this are skipped (the model only ever sees a region)
HIGH_RISK_MARKERS = ("auth", "crypto", "password", "secret", "migrate", "drop table", "rm -rf")

logger = get_logger("coding")


def _tag(investigation) -> str:
    sid = (investigation or {}).get("signal_id") if isinstance(investigation, dict) else None
    return f" [trace {sid}]" if sid else ""


def _bind(state) -> None:
    """Tag this node's log lines with the trace of the error being fixed (the signal id)."""
    inv = state.get("investigation") or {}
    sig = state.get("signal")
    bind_trace(inv.get("signal_id") or (sig.get("id") if isinstance(sig, dict) else None))


class State(TypedDict, total=False):
    incident_id: str
    signal: dict
    investigation: dict
    status: str  # completed | blocked | failed
    failure_reason: str
    clarification_needed: list
    worktree: str
    branch: str
    file_context: dict
    changed_files: list[str]
    diff_text: str
    tests: list[dict]
    checks: list[dict]
    round: int               # fix round, 1-based: a failed check can send the work back to fix once more
    test_output: str         # tail of the failing check, shown to the model on the next round
    no_more_edits: bool
    high_risk: bool          # auth/secret/migration markers, a very large diff, or a failed edit
    remaining_risks: list[str]
    requires_approval: bool
    context_snippets: dict   # read-only files the investigation read (shown to the model, never edited)
    added_tests: list[str]   # regression test files this run created
    review_result: dict      # the self-review verdict
    review_feedback: str     # reviewer's problems, shown to the model on the next fix round
    report: dict


def _blocked(incident_id: str, reason: str, needed: list[str]) -> CodingResult:
    return {
        "incident_id": incident_id,
        "status": "blocked",
        "root_cause_ref": "",
        "changed_files": [],
        "summary": f"blocked: {reason[:300]}",
        "tests": [],
        "validation": {"diff_nonempty": False, "tests_passed": False, "lint_passed": None},
        "remaining_risks": [],
        "requires_approval": False,
        "worktree": "",
        "branch": "",
        "failure_reason": reason,
        "clarification_needed": needed,
    }


def _failed(incident_id: str, reason: str, state: State) -> CodingResult:
    return {
        "incident_id": incident_id,
        "status": "failed",
        "root_cause_ref": str((state.get("investigation") or {}).get("root_cause", ""))[:200],
        "changed_files": state.get("changed_files") or [],
        "summary": f"failed: {reason[:300]}",
        "tests": state.get("tests") or [],
        "validation": {
            "diff_nonempty": bool((state.get("diff_text") or "").strip()),
            "tests_passed": False,
            "lint_passed": None,
        },
        "remaining_risks": state.get("remaining_risks") or [reason[:200]],
        "requires_approval": True,
        "worktree": state.get("worktree") or "",
        "branch": state.get("branch") or "",
        "failure_reason": reason,
    }


def _node_validate(state: State) -> State:
    _bind(state)
    ok, details = validate(state.get("incident_id", ""), state.get("signal"), state.get("investigation"))
    if not ok:
        step(logger, "coding.01.validate", "input blocked", level="warning",
             incident_id=state.get("incident_id"), reason=details["failure_reason"][:300])
        return {
            "status": "blocked",
            "failure_reason": details["failure_reason"],
            "clarification_needed": details["clarification_needed"],
        }
    step(logger, "coding.01.validate", "input valid", incident_id=state.get("incident_id"),
         affected_files=details["clean_investigation"].get("affected_files"))
    return {"investigation": details["clean_investigation"], "status": "validated"}


def _node_setup(state: State) -> State:
    _bind(state)
    if state.get("status") == "blocked":
        return {}
    incident_id = state["incident_id"]
    try:
        g.sync_repo()  # best-effort; ignored errors inside
        wt, branch = g.create_worktree(incident_id)
    except RuntimeError as e:
        step(logger, "coding.02.setup", "worktree setup failed", level="error",
             incident_id=incident_id, error=str(e)[:300])
        return {"status": "failed", "failure_reason": f"worktree setup failed: {e}"}
    step(logger, "coding.02.setup", "worktree ready", incident_id=incident_id,
         worktree=str(wt), branch=branch)
    # Refuse to work on a dirty tree (never overwrite unrelated changes).
    if g.diff(wt).strip():
        return {
            "status": "failed",
            "failure_reason": "worktree is dirty at setup - refusing to overwrite",
            "worktree": str(wt),
            "branch": branch,
        }
    return {"worktree": str(wt), "branch": branch, "status": "setup"}


def _node_context(state: State) -> State:
    _bind(state)
    if state.get("status") in ("blocked", "failed"):
        return {}
    wt = Path(state["worktree"])
    inv = state["investigation"]
    ctx: dict[str, str] = {}
    missing: list[str] = []
    for rel in inv.get("affected_files", [])[:10]:
        try:
            p = g.safe_path(wt, str(rel))
            if not p.exists():
                missing.append(f"{rel}: not found in worktree")
                continue
            text = p.read_bytes().decode("utf-8", errors=patch.ENC)  # bytes: keep line endings, never cp1252
            if len(text) > MAX_CONTEXT_CHARS:
                missing.append(f"{rel}: too large to patch safely ({len(text)} chars > {MAX_CONTEXT_CHARS})")
                continue
            ctx[str(rel)] = text
        except (ValueError, OSError) as e:
            missing.append(f"{rel}: {e}")
    step(logger, "coding.03.context", "affected files read", level="info" if ctx else "error",
         read=list(ctx), missing=missing)
    if missing and not ctx:
        return {"status": "failed", "failure_reason": "; ".join(missing)[:500]}
    out: State = {"file_context": ctx, "status": "context", "context_snippets": _context_snippets(wt, inv, set(ctx))}
    if missing:
        out["remaining_risks"] = missing
    return out


MAX_CONTEXT_FILES = 5
MAX_CONTEXT_LINES = 200


def _context_snippets(wt: Path, inv: dict, skip: set[str]) -> dict[str, str]:
    """Files the investigation read only to understand the bug (context_files): shown read-only to the fix step."""
    from .validate import _is_allowed_rel_path

    out: dict[str, str] = {}
    for rel in (inv.get("context_files") or [])[:MAX_CONTEXT_FILES]:
        if str(rel) in skip or not _is_allowed_rel_path(str(rel))[0]:
            continue
        try:
            text = g.safe_path(wt, str(rel)).read_bytes().decode("utf-8", errors=patch.ENC)
        except (ValueError, OSError):
            continue
        out[str(rel)] = NL.join(text.splitlines()[:MAX_CONTEXT_LINES])[:8000]
    return out


def _ask_llm(prompt: str) -> str:
    """One bounded LLM call (no tools); transient errors are retried."""
    last_err = ""
    for _ in range(max(1, settings.coding_llm_retries)):
        try:
            return text_of(get_llm().invoke(redact(prompt, kv=False))).strip()  # last line of defence: no keys to the model
        except Exception as e:  # transient LLM error -> retry
            last_err = str(e)
            time.sleep(1)
    raise RuntimeError(f"LLM unavailable: {last_err}"[:300])


def _node_fix(state: State) -> State:
    """Patch the affected files with small exact-match edits (see agents/coding/patch.py)."""
    _bind(state)
    if state.get("status") in ("blocked", "failed"):
        return {}
    wt = Path(state["worktree"])
    inv = dict(state["investigation"])
    inv.setdefault("log_evidence", (state.get("signal") or {}).get("evidence") or [])
    inv["context_snippets"] = state.get("context_snippets") or {}
    ctx = state.get("file_context") or {}
    retry = state.get("round", 1) > 1
    if retry:  # the files on disk already carry the first attempt: patch THEM, told why the checks failed
        ctx = {rel: (wt / rel).read_bytes().decode("utf-8", errors=patch.ENC) for rel in ctx if (wt / rel).is_file()}
        inv["test_failure"] = state.get("test_output", "")
        inv["review_feedback"] = state.get("review_feedback", "")
    try:
        outcome = patch.fix_files(wt, ctx, inv, _ask_llm, rounds=max(1, settings.coding_max_fix_iters))
    except (ValueError, OSError, RuntimeError) as e:
        step(logger, "coding.04.fix", "patching failed", level="error", error=f"{type(e).__name__}: {e}"[:300])
        return {"status": "failed", "failure_reason": f"fix failed: {e}"[:500]}
    diff_text = g.diff(wt)
    changed = _changed_files(diff_text)
    risks = list(state.get("remaining_risks") or []) + outcome["failures"]
    step(logger, "coding.04.fix", "patches applied in worktree", edits=outcome["edits_applied"],
         changed_files=changed, diff_lines=len(diff_text.splitlines()), failures=outcome["failures"],
         level="info" if changed else "warning")
    if not changed:
        reason = "; ".join(outcome["failures"]) or "the model proposed no applicable edits"
        return {"status": "failed", "failure_reason": f"no change made: {reason}"[:500], "remaining_risks": risks}
    if retry and not outcome["edits_applied"]:  # nothing new to try: do not loop on the same failure
        step(logger, "coding.04.fix", "retry round produced no new edit, stopping", level="warning")
        return {"status": "fixed", "no_more_edits": True, "remaining_risks": risks}
    low = (diff_text + inv.get("remediation", "")).lower()
    needs_human = any(m in low for m in HIGH_RISK_MARKERS) or len(diff_text.splitlines()) > 500 or bool(outcome["failures"])
    return {
        "diff_text": diff_text,
        "changed_files": changed,
        "checks": list({c["command"]: c for c in (state.get("checks") or []) + outcome["checks"]}.values()),
        "remaining_risks": risks,
        "requires_approval": bool(needs_human),
        "high_risk": bool(needs_human),
        "status": "fixed",
    }


def _changed_files(diff_text: str) -> list[str]:
    return [c.split(" b/")[-1] for c in (l[6:] for l in diff_text.splitlines() if l.startswith("diff --git"))]


SAFE_ENV_NAMES = {"PATH", "HOME", "USER", "USERNAME", "USERPROFILE", "LANG", "LANGUAGE", "TERM", "TZ", "TMP", "TEMP",
                  "TMPDIR", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "WINDIR", "PROGRAMFILES",
                  "PROGRAMDATA", "APPDATA", "LOCALAPPDATA", "CI", "NODE_ENV", "VIRTUAL_ENV", "SHELL", "PWD", "OS",
                  "GOPATH", "GOROOT", "GOFLAGS", "GOCACHE", "GOMODCACHE"}
SAFE_ENV_PREFIXES = ("LC_", "NODE_", "JAVA_", "CARGO_", "RUSTUP_", "DOTNET_", "PYTHON", "GRADLE_", "MAVEN_")
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "PRIVATE", "COOKIE", "AUTH")


def check_env() -> dict[str, str]:
    """Environment for lint/test commands: tool paths and locale only. The agent's own secrets (API keys, tokens,
    database URLs, SMTP passwords) are NOT passed to code the model wrote. CODING_TEST_ENV_ALLOW adds names."""
    allow = {n.strip() for n in os.getenv("CODING_TEST_ENV_ALLOW", "").split(",") if n.strip()}
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        up = name.upper()
        if name in allow or ((up in SAFE_ENV_NAMES or up.startswith(SAFE_ENV_PREFIXES))
                             and not any(m in up for m in SECRET_MARKERS)):
            env[name] = value
    env.setdefault("CI", "true")
    return env


def _no_network_argv(cmd: str) -> list[str] | None:
    """`unshare -rn sh -c cmd` (no network) when CODING_TEST_NO_NETWORK=true and the host supports it."""
    if os.getenv("CODING_TEST_NO_NETWORK", "false").strip().lower() != "true":
        return None
    if sys.platform.startswith("linux") and shutil.which("unshare"):
        return ["unshare", "-rn", "sh", "-c", cmd]
    step(logger, "coding.05.verify", "CODING_TEST_NO_NETWORK is set but this host cannot isolate the network",
         level="warning")
    return None


def _run_check(cmd: str, wt: Path) -> dict:
    try:
        argv = _no_network_argv(cmd)
        r = subprocess.run(argv if argv else cmd, shell=argv is None, cwd=wt, env=check_env(), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=settings.coding_test_timeout)
        return {"command": cmd, "exit_code": r.returncode, "passed": r.returncode == 0,
                "output_tail": (r.stdout + r.stderr)[-3000:]}
    except subprocess.TimeoutExpired:
        return {"command": cmd, "exit_code": None, "passed": False, "output_tail": "timeout"}


def _node_add_tests(state: State) -> State:
    """Add a regression test for the fix (new files only), keep it only if the suite passes with it."""
    _bind(state)
    if state.get("status") in ("blocked", "failed") or state.get("round", 1) > 1:
        return {}
    if os.getenv("CODING_WRITE_TESTS", "true").strip().lower() == "false" or not settings.test_cmd:
        return {}
    wt = Path(state["worktree"])
    risks = list(state.get("remaining_risks") or [])
    try:
        out = tests_gen.add_regression_tests(wt, state["investigation"], state.get("diff_text") or "", _ask_llm)
    except (ValueError, OSError, RuntimeError) as e:
        step(logger, "coding.04b.tests", "regression test generation failed", level="warning",
             error=f"{type(e).__name__}: {e}"[:200])
        return {"remaining_risks": risks + [f"no regression test was added: {e}"[:200]]}
    risks += [f"regression test not added: {n}" for n in out["notes"]]
    if not out["files"]:
        if not out["notes"]:
            risks.append("no regression test was added (the model could not write a reliable one)")
        return {"remaining_risks": risks}
    pre = _run_check(settings.test_cmd, wt)
    if not pre["passed"]:  # a test that does not pass must not break an otherwise good fix: drop it, say so
        tests_gen.remove(wt, out["files"])
        step(logger, "coding.04b.tests", "generated test failed validation, dropped", level="warning",
             files=out["files"])
        return {"remaining_risks": risks + ["a generated regression test failed validation and was dropped: "
                                            + pre["output_tail"][-150:].strip()]}
    diff_text = g.diff(wt)
    step(logger, "coding.04b.tests", "regression test added", files=out["files"])
    return {"diff_text": diff_text, "changed_files": _changed_files(diff_text), "added_tests": out["files"],
            "remaining_risks": risks}


def _fix_rounds() -> int:
    try:
        return max(1, int(os.getenv("CODING_FIX_ROUNDS", "2")))
    except ValueError:
        return 2


def _node_verify(state: State) -> State:
    """LINT_CMD and TEST_CMD (when set) plus the syntax checks. A failing check sends the work back to fix once more."""
    _bind(state)
    if state.get("status") in ("blocked", "failed"):
        return {}
    wt = Path(state["worktree"])
    commands = [c for c in (os.getenv("LINT_CMD", "").strip(), settings.test_cmd) if c]
    checks = list(state.get("checks") or [])  # syntax checks from the patch step
    if not settings.test_cmd and os.getenv("CODING_REQUIRE_TESTS", "false").strip().lower() == "true":
        step(logger, "coding.05.verify", "no TEST_CMD and CODING_REQUIRE_TESTS=true", level="error")
        return {"status": "failed", "failure_reason": "no TEST_CMD is configured and CODING_REQUIRE_TESTS=true: "
                                                       "a fix with no tests behind it is refused"}
    if not settings.test_cmd:
        step(logger, "coding.05.verify", "TEST_CMD empty: syntax checks only", level="warning",
             syntax_checks=len(checks), passed=all(c["passed"] for c in checks))
        risks = list(state.get("remaining_risks") or []) + ["no TEST_CMD configured: tests were not run"]
        tests = [_run_check(c, wt) for c in commands] + checks
        return {"tests": tests, "status": "verified", "requires_approval": True, "remaining_risks": risks}
    tests = [_run_check(c, wt) for c in commands] + checks  # lint, then tests, then syntax checks
    ok = all(t["passed"] for t in tests)
    step(logger, "coding.05.verify", "checks finished", level="info" if ok else "error",
         commands=commands, passed=ok, round=state.get("round", 1), syntax_checks=len(checks))
    failing = next((t for t in tests if not t["passed"]), None)
    can_retry = (failing is not None and failing.get("exit_code") is not None and not state.get("no_more_edits")
                 and state.get("round", 1) < _fix_rounds())
    if can_retry:  # timeouts are environment problems, not something another edit fixes
        step(logger, "coding.05.verify", "check failed, sending the failure back to fix", level="warning",
             failing=failing["command"], next_round=state.get("round", 1) + 1)
        return {"tests": tests, "status": "retry", "round": state.get("round", 1) + 1, "review_feedback": "",
                "test_output": f"{failing['command']} exited {failing['exit_code']}:" + chr(10) + failing["output_tail"][-1500:]}
    need_human = state.get("requires_approval") or not ok
    return {"tests": tests, "status": "verified", "requires_approval": bool(need_human)}


def _node_review(state: State) -> State:
    """A second model call reviews the diff. A fail goes back to fix once more; a repeat fail is held for a human."""
    _bind(state)
    if state.get("status") != "verified" or os.getenv("CODING_SELF_REVIEW", "true").strip().lower() == "false":
        return {}
    risks = list(state.get("remaining_risks") or [])
    try:
        verdict = review.review(state["investigation"], state.get("diff_text") or "", state.get("tests") or [],
                                _ask_llm)
    except (ValueError, RuntimeError) as e:
        step(logger, "coding.05b.review", "self-review did not run", level="warning",
             error=f"{type(e).__name__}: {e}"[:200])
        return {"remaining_risks": risks + [f"self-review did not run: {e}"[:200]], "requires_approval": True}
    step(logger, "coding.05b.review", "self-review done", verdict=verdict["verdict"], problems=verdict["problems"],
         round=state.get("round", 1), level="info" if verdict["verdict"] == "pass" else "warning")
    if verdict["verdict"] == "pass":
        return {"review_result": verdict}
    if not state.get("no_more_edits") and state.get("round", 1) < _fix_rounds():
        return {"review_result": verdict, "status": "retry", "round": state.get("round", 1) + 1, "test_output": "",
                "review_feedback": NL.join(f"- {p}" for p in verdict["problems"])}
    return {"review_result": verdict, "requires_approval": True,
            "remaining_risks": risks + [f"reviewer: {p}" for p in verdict["problems"]]}


def _node_report(state: State) -> State:
    _bind(state)
    if state.get("status") == "blocked":
        return {}  # run() handles blocked terminal
    if state.get("status") == "failed":
        return {}
    inv = state.get("investigation") or {}
    tests = state.get("tests") or []
    diff_text = state.get("diff_text") or ""
    all_passed = bool(tests) and all(t["passed"] for t in tests)
    tests_ran = bool(settings.test_cmd)
    verdict = ("tests pass" if tests_ran else "syntax OK, tests NOT run (no TEST_CMD)") if all_passed else "checks FAILED"
    n_lines = len(diff_text.splitlines())
    result: CodingResult = {
        "incident_id": state["incident_id"],
        "status": "completed" if (all_passed and diff_text.strip()) else "failed",
        "root_cause_ref": str(inv.get("root_cause", ""))[:200],
        "changed_files": state.get("changed_files") or [],
        "summary": (
            f"fix {state.get('branch', '')}: {len(state.get('changed_files') or [])} file(s), "
            f"{n_lines} diff line(s), {verdict}"
        )[:500],
        "tests": tests,
        "validation": {
            "diff_nonempty": bool(diff_text.strip()),
            "tests_passed": all_passed and tests_ran,
            "tests_ran": tests_ran,
            "syntax_ok": all(c.get("passed") for c in (state.get("checks") or [])),
            "lint_passed": None,
        },
        "remaining_risks": list(state.get("remaining_risks") or []) + list(inv.get("risks") or []),
        "requires_approval": bool(state.get("requires_approval") or not (all_passed and tests_ran)),
        "worktree": state.get("worktree") or "",
        "branch": state.get("branch") or "",
        "high_risk": bool(state.get("high_risk")),
        "added_tests": state.get("added_tests") or [],
        "review": (state.get("review_result") or {}).get("verdict"),
    }
    step(logger, "coding.06.report", "result built", level="info" if result["status"] == "completed" else "error",
         status=result["status"], summary=result["summary"], requires_approval=result["requires_approval"])
    if result["status"] == "failed":
        result["failure_reason"] = "tests failed or empty diff - see tests[].output_tail"
    return {"report": result}  # type: ignore[typeddict-unknown-key]


def build():
    gph = StateGraph(State)
    for name, fn in [
        ("validate", _node_validate),
        ("setup", _node_setup),
        ("context", _node_context),
        ("fix", _node_fix),
        ("add_tests", _node_add_tests),
        ("verify", _node_verify),
        ("review", _node_review),
        ("report", _node_report),
    ]:
        gph.add_node(name, fn)
    gph.add_edge(START, "validate")
    gph.add_edge("validate", "setup")
    gph.add_edge("setup", "context")
    gph.add_edge("context", "fix")
    gph.add_edge("fix", "add_tests")
    gph.add_edge("add_tests", "verify")
    gph.add_conditional_edges("verify", lambda st: "fix" if st.get("status") == "retry" else "review",
                              {"fix": "fix", "review": "review"})
    gph.add_conditional_edges("review", lambda st: "fix" if st.get("status") == "retry" else "report",
                              {"fix": "fix", "report": "report"})
    gph.add_edge("report", END)
    return gph.compile()


def _resolve_arg(value, kind: str | None = None):
    """Accept an inline dict or a DB item id (supervisor may pass either)."""
    if isinstance(value, dict) or value is None:
        return value
    if isinstance(value, str) and kind:
        item = get_item(value)
        if item is not None:
            return item["data"]
    return value


def _discard(out: State) -> None:
    """A run that produced no fix leaves nothing behind: its worktree and its (never shipped) branch are removed.
    The diff it tried is kept on the coding_result record. CODING_KEEP_FAILED=true keeps the worktree to inspect."""
    if out.get("worktree") and os.getenv("CODING_KEEP_FAILED", "false").strip().lower() != "true":
        g.cleanup_worktree(out["worktree"], out.get("branch", ""), delete_branch=True)


def run(incident_id: str = "", signal: dict | str | None = None, investigation: dict | str | None = None) -> dict:
    """Entry point (auto-discovered by run.py / server.py).

    Args:
        incident_id: correlates to the monitor signal item id.
        signal: monitor Signal dict (or its DB item id).
        investigation: InvestigationReport dict (or its DB item id).
    Returns:
        CodingResult dict (JSON-serializable). Also persisted as kind="coding_result".
    """
    signal_d = _resolve_arg(signal, "signal")
    inv_d = _resolve_arg(investigation, "investigation")
    if isinstance(inv_d, dict) and not incident_id:
        incident_id = str(inv_d.get("incident_id") or "")

    out = build().invoke({"incident_id": incident_id, "signal": signal_d, "investigation": inv_d})

    if out.get("status") == "blocked":
        result = _blocked(incident_id, out.get("failure_reason", "blocked"), out.get("clarification_needed", []))
        create_item(KIND, dict(result))
        logger.warning("coding.blocked", extra={"incident_id": incident_id,
                                                "reason": out.get("failure_reason", "blocked")[:200]})
        return dict(result)
    if out.get("status") == "failed" or "report" not in out:
        result = _failed(incident_id, out.get("failure_reason", "failed without report"), out)
        create_item(KIND, {**result, "failed_diff": (out.get("diff_text") or "")[:20000]})
        logger.error("coding.failed", extra={"incident_id": incident_id,
                                             "reason": out.get("failure_reason", "")[:200],
                                             "branch": out.get("branch", "")})
        notify(f"coding failed: {incident_id}{_tag(inv_d)}", result.get("failure_reason", "")[:500])
        _discard(out)
        return {**result, "worktree": ""}
    result = dict(out["report"])
    if result.get("status") != "completed":  # checks failed or the diff is empty: nothing worth keeping
        create_item(KIND, {**result, "failed_diff": (out.get("diff_text") or "")[:20000]})
        notify(f"coding failed: {incident_id}{_tag(inv_d)}", result.get("failure_reason", "")[:500])
        _discard(out)
        return {**result, "worktree": ""}
    create_item(KIND, result)
    logger.info("coding.completed", extra={"incident_id": incident_id, "branch": result.get("branch", ""),
                                           "changed_files": result.get("changed_files", []),
                                           "summary": result.get("summary", "")[:200]})
    notify(f"coding done: {incident_id}{_tag(inv_d)}", result.get("summary", "")[:500])
    return result

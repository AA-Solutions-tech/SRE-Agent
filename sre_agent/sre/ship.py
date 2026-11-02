"""Ship a finished fix: commit it, push its branch, open a pull request. Never merges or deploys.

The coding agent only stages a change in its worktree. When it finishes with status completed, `offer()` ships it:
    SHIP_MODE=auto (default)  commit, push and raise the pull request at once, then email a "fix shipped" summary.
                              A high-risk change (auth, secrets, migrations, a very large diff, a failed edit)
                              is held and sent as an approval card instead.
    SHIP_MODE=approve         always send the approval card first; shipping happens on Approve.
Shipping = commit on the ai-fix branch (bot identity, no model names), push to origin ONLY when
DRY_RUN_PUSH=false, open a pull request into BASE_BRANCH when GITHUB_TOKEN is set (a draft when no tests ran),
else give the compare link. The pull request is the review gate: a person merges it.
"""
import os
import re
from pathlib import Path

import httpx

from sre_agent.core.approvals import on_approval, request_approval
from sre_agent.core.config import settings
from sre_agent.core.db import create_item, get_item, update_item
from sre_agent.core.git_tools import _git, diff as staged_diff
from sre_agent.core.notify import notify
from sre_agent.core.sre_log import bind_trace, get_logger, step

STAGE = "ship_fix"
logger = get_logger("ship")
NL = chr(10)
PATCH_CHARS = 5000
REMOTE_RE = re.compile(r"(?:git@|ssh://git@|https://(?:[^@/]+@)?)github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def enabled() -> bool:
    return os.getenv("SHIP_ENABLED", "true").strip().lower() not in ("false", "0", "no", "off")


def mode() -> str:
    """auto (default): ship without asking. approve: send the approval card first."""
    return "approve" if os.getenv("SHIP_MODE", "auto").strip().lower() == "approve" else "auto"


def parse_remote(url: str) -> tuple[str, str] | None:
    """(owner, repo) from a GitHub remote URL, or None for any other host."""
    m = REMOTE_RE.search((url or "").strip())
    return (m.group(1), m.group(2)) if m else None


def commit_message(inv: dict, branch: str) -> str:
    error = str(inv.get("error", "error")).strip()[:60]
    lines = [f"fix({inv.get('service', 'app')}): {error}", "",
             f"Root cause: {str(inv.get('root_cause', ''))[:400]}",
             f"Fix: {str(inv.get('remediation', ''))[:400]}", "",
             f"Branch {branch}, plan version {inv.get('version', 1)}, investigation {inv.get('_id', '')}"]
    return NL.join(lines)


def pr_body(inv: dict, result: dict) -> str:
    checks = NL.join(f"- {'pass' if t.get('passed') else 'FAIL'}: {t.get('command') or 'no command'}"
                     for t in result.get("tests") or []) or "- none ran"
    risks = NL.join(f"- {r}" for r in result.get("remaining_risks") or []) or "- none recorded"
    return NL.join([
        "Prepared automatically from a production error. A person must review and merge this pull request.", "",
        f"**Error:** {inv.get('error')} ({inv.get('service')})", f"**Root cause:** {inv.get('root_cause')}", "",
        f"**Fix:** {inv.get('remediation')}", "", "**Checks**", checks, "", "**Remaining risks**", risks])


def _diff_view(wt: Path) -> tuple[str, str]:
    stat = _git(wt, "diff", "--cached", "--stat", check=False)
    patch = _git(wt, "diff", "--cached", "-U2", check=False)
    return stat, patch[:PATCH_CHARS] + (NL + "... (diff shortened)" if len(patch) > PATCH_CHARS else "")


def _summary_parts(result: dict) -> tuple[str, str, str, str]:
    """(checks text, tests note, diff stat, risks text) shared by both emails."""
    checks = NL.join(f"{'pass' if t.get('passed') else 'FAIL'}  {t.get('command') or 'no command'}"
                     for t in result.get("tests") or []) or "none ran"
    note = "" if settings.test_cmd else "NOTE: no TEST_CMD is set, so only syntax checks ran, no tests." + NL
    risks = NL.join(f"• {r}" for r in (result.get("remaining_risks") or [])[:6]) or "• none recorded"
    return checks, note, _diff_view(Path(result["worktree"]))[0], risks


def offer(result: dict, investigation_id: str) -> str | None:
    """Ship (auto mode) or ask (approve mode / held for risk) for a completed coding result."""
    if not enabled() or result.get("status") != "completed" or not result.get("worktree"):
        return None
    inv_item = get_item(investigation_id)
    if inv_item is None:
        return None
    inv = inv_item["data"]
    bind_trace(inv.get("signal_id"))
    wt = Path(result["worktree"])
    staged_diff(wt)  # stage everything so the diff below is what a commit would contain
    checks, tests_note, stat, risks = _summary_parts(result)
    fix_id = create_item("fix_ready", {
        "investigation_id": investigation_id, "signal_id": inv.get("signal_id"), "branch": result.get("branch"),
        "worktree": result.get("worktree"), "changed_files": result.get("changed_files") or [],
        "summary": result.get("summary"), "tests": result.get("tests") or [],
        "remaining_risks": result.get("remaining_risks") or [], "service": inv.get("service"),
        "error": inv.get("error"), "root_cause": inv.get("root_cause"), "remediation": inv.get("remediation"),
        "version": inv.get("version", 1),
    })
    tag = f"[trace {inv.get('signal_id')}]"
    held = bool(result.get("high_risk"))
    if mode() == "auto" and not held:
        msg = ship_fix(fix_id)
        body = NL.join([
            f"*FIX SHIPPED*  {inv.get('service')} · {inv.get('error')}",
            f"Branch `{result.get('branch')}` · plan version {inv.get('version', 1)} · "
            f"{len(result.get('changed_files') or [])} file(s) changed", "", msg, "",
            tests_note + "*CHECKS*", checks, "", "*WHAT IT CHANGES*", stat, "", "*REMAINING RISKS*", risks, "",
            "Review the pull request and merge it when you are satisfied. Nothing is merged or deployed automatically.",
        ])
        notify(f"fix shipped: {inv.get('service')} {inv.get('error')} [{result.get('branch')}] {tag}", body)
        step(logger, "ship.01.offer", "fix shipped automatically", fix_id=fix_id, branch=result.get("branch"),
             result=msg[:200])
        return fix_id
    patch = _diff_view(wt)[1]
    push = "will push the branch" if not settings.dry_run_push else "will NOT push (DRY_RUN_PUSH=true): commit only"
    pr = "opens a pull request" if settings.github_token else "gives you the pull request link (no GITHUB_TOKEN)"
    why = ("HELD FOR APPROVAL: high-risk change (touches auth, secrets or migrations, is very large, or an edit "
           "failed). In auto mode this is the only kind that waits." + NL) if held else ""
    body = NL.join([
        f"*FIX READY FOR REVIEW*  {inv.get('service')} · {inv.get('error')}",
        f"Branch `{result.get('branch')}` · plan version {inv.get('version', 1)} · "
        f"{len(result.get('changed_files') or [])} file(s) changed",
        "", why + tests_note + "*CHECKS*", checks, "", "*WHAT IT CHANGES*", stat, "", "*DIFF*", patch, "",
        "*REMAINING RISKS*", risks, "",
        "*ON APPROVE*",
        f"• Commits the change on {result.get('branch')}, {push}, and {pr} into {settings.base_branch}.",
        "• Nothing is merged or deployed by this step.",
        "*ON REJECT*", "• Nothing is committed; the worktree is kept so you can look at it.",
    ])
    request_approval(fix_id, STAGE, f"fix ready: {inv.get('service')} {inv.get('error')} "
                                    f"[{result.get('branch')}] {tag}", body)
    step(logger, "ship.01.offer", "fix ready card sent" + (" (held: high risk)" if held else ""), fix_id=fix_id,
         investigation_id=investigation_id, branch=result.get("branch"),
         files=len(result.get("changed_files") or []))
    return fix_id


def open_pull_request(d: dict, inv_like: dict, result_like: dict) -> str:
    """Create the pull request (needs GITHUB_TOKEN and a github.com origin), else return the compare link.
    It is opened as a draft when no tests ran, so a reviewer cannot mistake it for a tested change."""
    remote = _git(Path(d["worktree"]), "remote", "get-url", "origin", check=False)
    slug = parse_remote(remote)
    if not slug:
        return f"(origin is not a github.com remote: {remote or 'none'}, open the pull request by hand)"
    owner, repo = slug
    compare = f"https://github.com/{owner}/{repo}/compare/{settings.base_branch}...{d['branch']}?expand=1"
    if not settings.github_token:
        return f"{compare} (no GITHUB_TOKEN: open the pull request from this link)"
    r = httpx.post(f"https://api.github.com/repos/{owner}/{repo}/pulls", timeout=20, json={
        "title": commit_message(inv_like, d["branch"]).split(NL)[0], "head": d["branch"], "base": settings.base_branch,
        "body": pr_body(inv_like, result_like), "draft": not settings.test_cmd},
        headers={"Authorization": f"Bearer {settings.github_token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    if r.status_code == 201:
        return r.json().get("html_url", compare)
    if r.status_code == 422:  # most often: a pull request for this branch already exists
        return f"{compare} (GitHub said: {str(r.json().get('message', ''))[:120]})"
    raise RuntimeError(f"GitHub returned {r.status_code}")


def ship_fix(item_id: str) -> str:
    """Commit, push (when allowed) and open the pull request for a fix_ready record. Returns what happened."""
    item = get_item(item_id)
    d = item["data"] if item else {}
    bind_trace(d.get("signal_id"))
    wt = Path(d.get("worktree", ""))
    if not wt.is_dir():
        return "cannot ship: the worktree is gone"
    if not staged_diff(wt).strip():
        return "nothing to commit: the change is already committed or was removed"
    inv = {**d, "_id": d.get("investigation_id")}
    _git(wt, "commit", "-m", commit_message(inv, d["branch"]))
    sha = _git(wt, "rev-parse", "--short", "HEAD", check=False)
    out = f"committed {sha} on {d['branch']}"
    update_item(item_id, outcome="committed", commit=sha)
    pr_url = ""
    if settings.dry_run_push:
        out += " (DRY_RUN_PUSH=true, so the branch was not pushed and no pull request was opened)"
    else:
        try:
            _git(wt, "push", "-u", "origin", d["branch"])
        except RuntimeError as e:
            step(logger, "ship.02.decision", "push failed after commit", level="error", fix_id=item_id,
                 error=str(e)[:200])
            return (out + f". The push failed ({str(e)[:160]}); fix the credentials, then run: "
                    f"git -C {wt} push -u origin {d['branch']}")
        out += ", pushed"
        try:
            pr_url = open_pull_request(d, inv, d)
            update_item(item_id, outcome="pushed", pr=pr_url)
            out += f". Pull request: {pr_url}"
        except (RuntimeError, httpx.HTTPError) as e:
            out += f". The pull request could not be opened ({str(e)[:120]}); open it from GitHub"
    _link_issue(d, sha, pr_url)
    step(logger, "ship.02.decision", "fix shipped", fix_id=item_id, branch=d["branch"], result=out[:200])
    return out


def _link_issue(d: dict, sha: str, pr_url: str) -> None:
    """Record the branch, commit and pull request on the issue, so /issues and reminders show them."""
    sig = get_item(d["signal_id"]) if d.get("signal_id") else None
    issue_id = (sig["data"] if sig else {}).get("issue_id")
    if issue_id and get_item(issue_id):
        update_item(issue_id, branch=d.get("branch"), commit=sha, pr_url=pr_url or None)


@on_approval(STAGE)
def handle_ship(item_id: str, action: str) -> str:
    item = get_item(item_id)
    d = item["data"] if item else {}
    bind_trace(d.get("signal_id"))
    if action != "approve":
        update_item(item_id, outcome="discarded")
        step(logger, "ship.02.decision", "fix discarded by a person", level="warning", fix_id=item_id)
        return f"discarded: nothing was committed. The worktree stays at {d.get('worktree')}"
    return ship_fix(item_id)

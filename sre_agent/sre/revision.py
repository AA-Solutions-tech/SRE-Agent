"""Plan revision: a reviewer asks for changes, the incident agent produces the next plan version.

Every plan version is its own `investigation` record (its own signed links). Asking for changes
creates version N+1 and marks version N superseded, so N's links stop working. What the reviewer
says is untrusted text: the model sees it quoted, and everything that must be obeyed (denied paths,
allowed paths, extra files, extra tests, ops-only) is a structured field applied by CODE.

    begin()   verify the link and the request, claim version N          (synchronous, raises on refusal)
    revise()  re-analyse, apply the constraints, store N+1, send card   (run it in the background)
"""
import hmac
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from sre_agent.core.config import settings
from sre_agent.core.db import create_item, get_item, update_item
from sre_agent.core.git_tools import BLOCKED_PARTS
from sre_agent.core.notify import approval_token, notify
from sre_agent.core.sre_log import bind_trace, get_logger, step
from sre_agent.sre import issues

logger = get_logger("revision")
STAGE = "incident_fix"  # same gate as sre.incident.publish.STAGE (literal here to avoid a circular import)
AWAITING = f"awaiting_{STAGE}"
REVISING = f"{STAGE}_revising"
SUPERSEDED = f"{STAGE}_superseded"
MAX_PATHS = 10
MAX_TESTS = 5
NL = chr(10)
ANALYSIS_KEYS = (
    "summary", "impact", "category", "fix_type", "root_cause", "remediation", "affected_files", "context_files",
    "estimated_lines", "code_refs", "verified_citations", "unverified_citations", "alternatives", "history",
    "evidence", "confidence", "risk", "required_tests", "next_steps", "pattern", "limited",
)
_lock = threading.Lock()
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


# ---------------- settings ----------------
def enabled() -> bool:
    return os.getenv("REVISION_ENABLED", "true").strip().lower() not in ("false", "0", "no", "off")


def reply_enabled() -> bool:
    """Reply-by-email switched on (EMAIL_REPLY_ENABLED=true)."""
    return os.getenv("EMAIL_REPLY_ENABLED", "false").strip().lower() in ("true", "1", "yes", "on")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def max_revisions() -> int:
    return _int_env("REVISION_MAX", 3)


def note_limit() -> int:
    return _int_env("REVISION_NOTE_MAX_CHARS", 2000)


def link_ttl() -> timedelta:
    return timedelta(hours=_int_env("REVISION_LINK_TTL_HOURS", 48))


def revisions_left(version: int) -> int:
    return max(0, max_revisions() - (version - 1))


def _repo() -> Path | None:
    return settings.repo_dir if (settings.repo_dir / ".git").exists() else None


# ---------------- the reviewer's structured fields ----------------
def clean_path(raw: str) -> str:
    """A repo-relative POSIX path, or ValueError. Refuses absolute paths, '..', and .git/.env/node_modules."""
    p = raw.strip().replace("\\", "/").removeprefix("./")
    if not p or len(p) > 200 or any(ord(c) < 32 for c in p):
        raise ValueError(f"not a usable path: {raw[:60]!r}")
    if p.startswith("/") or _WINDOWS_DRIVE.match(p):
        raise ValueError(f"absolute paths are not allowed: {p}")
    parts = PurePosixPath(p).parts
    if ".." in parts or BLOCKED_PARTS & set(parts):
        raise ValueError(f"path is not allowed: {p}")
    return p.rstrip("/")


def _lines(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"[\r\n,]+", text or "") if x.strip()]


def parse_constraints(fields: dict) -> tuple[dict, list[str]]:
    """Form fields -> (constraints, errors). Nothing here trusts the text beyond its shape."""
    out: dict = {"allow_paths": [], "deny_paths": [], "extra_files": [], "extra_tests": [], "force_fix_type": None}
    errors: list[str] = []
    for key in ("allow_paths", "deny_paths", "extra_files"):
        for raw in _lines(str(fields.get(key, "")))[:MAX_PATHS]:
            try:
                out[key].append(clean_path(raw))
            except ValueError as e:
                errors.append(f"{key}: {e}")
        if len(_lines(str(fields.get(key, "")))) > MAX_PATHS:
            errors.append(f"{key}: at most {MAX_PATHS} paths")
    tests = [t[:200] for t in re.split(r"[\r\n]+", str(fields.get("extra_tests", ""))) if t.strip()]
    out["extra_tests"] = [t.strip() for t in tests][:MAX_TESTS]
    if str(fields.get("ops_only", "")).lower() in ("on", "true", "1", "yes"):
        out["force_fix_type"] = "ops"
    return out, errors


def has_constraints(c: dict) -> bool:
    return bool(c.get("allow_paths") or c.get("deny_paths") or c.get("extra_files")
                or c.get("extra_tests") or c.get("force_fix_type"))


def _under(path: str, prefixes: list[str]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def apply_constraints(analysis: dict, constraints: dict, repo: Path | None) -> tuple[dict, list[str]]:
    """Enforce the reviewer's fields on an analysis, in code. Returns (analysis, notes about what was done)."""
    a = {**analysis}
    notes: list[str] = []
    files = list(a.get("affected_files") or [])
    deny, allow = constraints.get("deny_paths") or [], constraints.get("allow_paths") or []
    removed = [f for f in files if _under(f, deny)]
    if removed:
        notes.append("removed files the reviewer said not to touch: " + ", ".join(removed))
    files = [f for f in files if f not in removed]
    if allow:
        outside = [f for f in files if not _under(f, allow)]
        if outside:
            notes.append("removed files outside the allowed folders: " + ", ".join(outside))
        files = [f for f in files if f not in outside]
    refs = [r for r in (a.get("code_refs") or []) if r.get("path") in files]
    for extra in constraints.get("extra_files") or []:
        if _under(extra, deny) or (allow and not _under(extra, allow)):
            notes.append(f"extra file {extra} conflicts with the allowed or denied paths: ignored")
        elif repo is None or not (repo / extra).is_file():
            notes.append(f"extra file {extra} was not found in the repository: ignored")
        elif extra not in files:
            files.append(extra)
            refs.append({"path": extra, "line": 1})
            notes.append(f"added {extra} at the reviewer's request (not cited by the analysis)")
    a["affected_files"], a["code_refs"] = files, refs
    tests = list(a.get("required_tests") or [])
    for t in constraints.get("extra_tests") or []:
        if t not in tests:
            tests.append(t)
            notes.append(f"added required test: {t}")
    a["required_tests"] = tests
    if constraints.get("force_fix_type") == "ops" and a.get("fix_type") != "ops":
        a["fix_type"] = "ops"
        notes.append("fix type set to operational by the reviewer: the coding agent will not run")
    if a.get("fix_type") == "code" and not files:
        a["confidence"] = min(float(a.get("confidence") or 0), 0.3)
        notes.append("no file left to edit after the constraints: confidence capped at 30%")
    return a, notes


def diff_versions(prev: dict, new: dict) -> list[str]:
    """Plain-language list of what differs between two analyses."""
    out: list[str] = []
    pf, nf = set(prev.get("affected_files") or []), set(new.get("affected_files") or [])
    if nf - pf:
        out.append("files now edited: " + ", ".join(sorted(nf - pf)))
    if pf - nf:
        out.append("files no longer edited: " + ", ".join(sorted(pf - nf)))
    if prev.get("fix_type") != new.get("fix_type"):
        out.append(f"fix type {prev.get('fix_type')} -> {new.get('fix_type')}")
    pc, nc = float(prev.get("confidence") or 0), float(new.get("confidence") or 0)
    if abs(nc - pc) >= 0.05:
        out.append(f"confidence {round(pc * 100)}% -> {round(nc * 100)}%")
    if (prev.get("root_cause") or "") != (new.get("root_cause") or ""):
        out.append("root cause re-written")
    if (prev.get("remediation") or "") != (new.get("remediation") or ""):
        out.append("recommended fix re-written")
    added = [t for t in (new.get("required_tests") or []) if t not in (prev.get("required_tests") or [])]
    if added:
        out.append("tests added: " + "; ".join(added))
    return out


# ---------------- verification and claiming (synchronous) ----------------
def check_link(item_id: str, version: int, token: str) -> dict:
    """Verify a Request changes link without changing anything. Returns the investigation item.

    Raises PermissionError (bad token), LookupError (unknown/off), ValueError (stale, handled, expired)."""
    if not enabled():
        raise LookupError("plan revisions are switched off")
    if not hmac.compare_digest(token or "", approval_token(item_id, f"feedback-v{version}")):
        raise PermissionError("invalid token")
    item = get_item(item_id)
    if item is None or item["kind"] != "investigation":
        raise LookupError("unknown plan")
    if item["data"].get("version", 1) != version:
        raise ValueError("this plan has been replaced by a newer version; use the newest email")
    if item["status"] == SUPERSEDED:
        raise ValueError("this plan has been replaced by a newer version; use the newest email")
    if item["status"] != AWAITING:
        raise ValueError(f"this plan was already handled ({item['status']})")
    created = datetime.fromisoformat(item["created_at"])
    if datetime.now(timezone.utc) - created > link_ttl():
        raise ValueError("this link has expired")
    return item


def begin(item_id: str, version: int, token: str, note: str, fields: dict) -> tuple[str, dict]:
    """Check a feedback request and claim plan version `version`. Returns (clean note, constraints).

    Raises PermissionError (bad token), LookupError (unknown/off), ValueError (refused, message is user-safe)."""
    item = check_link(item_id, version, token)
    data = item["data"]
    bind_trace(data.get("signal_id"))
    note = (note or "").strip()
    if len(note) > note_limit():
        raise ValueError(f"the note is too long (limit {note_limit()} characters)")
    constraints, errors = parse_constraints(fields)
    if errors:
        raise ValueError("; ".join(errors))
    if not note and not has_constraints(constraints):
        raise ValueError("write a note or set at least one option")
    if version - 1 >= max_revisions():
        _escalate(item_id, data, version)
        raise ValueError(f"the revision limit ({max_revisions()}) is reached; a person has been told, "
                         "please approve, reject or fix it yourself")
    with _lock:  # two clicks at once must not both revise the same version
        fresh = get_item(item_id)
        if fresh["status"] != AWAITING:
            raise ValueError("this plan is already being revised")
        update_item(item_id, status=REVISING)
    step(logger, "incident.06.revise", "revision requested", investigation_id=item_id, version=version,
         note_chars=len(note), constraints=sorted(k for k, v in constraints.items() if v))
    return note, constraints


def _escalate(item_id: str, data: dict, version: int) -> None:
    chain, cur, seen = [], data, 0
    while cur and seen < 10:
        chain.append(f"version {cur.get('version', 1)}: {str(cur.get('instruction') or 'original analysis')[:200]}")
        cur = get_item(cur["previous_id"])["data"] if cur.get("previous_id") else None
        seen += 1
    notify(f"revision limit reached: {data.get('service')} {data.get('error')}",
           NL.join([f"Plan version {version} is still waiting, and its {max_revisions()} allowed revisions are used.",
                    "A person should approve, reject or fix it themselves.", "", *reversed(chain)]))
    step(logger, "incident.06.revise", "revision limit reached, escalated", level="warning",
         investigation_id=item_id, version=version)


# ---------------- producing version N+1 (run in the background) ----------------
def _rca_text(a: dict) -> str:
    return (f"Likely cause: {a.get('root_cause', '')}{NL}Files: {', '.join(a.get('affected_files') or [])} "
            f"(confidence {float(a.get('confidence') or 0):.2f}){NL}Suggested fix: {a.get('remediation', '')}")


def _reanalyse(signal: dict, signal_id: str, data: dict, note: str, constraints: dict) -> dict:
    """Ask the incident agent's analysis again with the reviewer's request; {} when it cannot."""
    from sre_agent.sre.incident.code_inspect import inspect
    from sre_agent.sre.incident.publish import related_signals

    revision = {"version": data.get("version", 1), "note": note, "constraints": constraints,
                "previous": {k: data.get(k) for k in ("summary", "root_cause", "affected_files", "remediation")}}
    try:
        return inspect(signal, related_signals(signal, signal_id), revision=revision) or {}
    except Exception as e:  # the constraints are still applied to the previous plan
        step(logger, "incident.06.revise", "re-analysis failed, applying constraints only", level="warning",
             error=f"{type(e).__name__}: {e}"[:200])
        return {}


def revise(item_id: str, note: str, constraints: dict) -> str:
    """Create plan version N+1 from version N (which `begin` already claimed). Returns the new id."""
    item = get_item(item_id)
    data = item["data"]
    version = data.get("version", 1)
    signal_item = get_item(data["signal_id"])
    bind_trace(signal_item["id"])
    signal = {**signal_item["data"], "id": signal_item["id"]}
    prev = {k: data.get(k) for k in ANALYSIS_KEYS if k in data}
    fresh = _reanalyse(signal, signal_item["id"], data, note, constraints)
    notes = []
    if not fresh.get("root_cause"):
        notes.append("the model could not re-analyse: the previous analysis was kept and only the "
                     "reviewer's constraints were applied")
    merged = {**prev, **{k: v for k, v in fresh.items() if k in ANALYSIS_KEYS}}
    analysis, applied = apply_constraints(merged, constraints, _repo())
    diffs = diff_versions(prev, analysis)
    applied = [n for n in applied  # a test the diff already lists is not repeated
               if not (n.startswith("added required test:") and any(n.split(": ", 1)[1] in d for d in diffs))]
    changes = diffs + applied + notes
    new_data = {
        **{k: data[k] for k in ("signal_id", "service", "severity", "error") if k in data},
        "rca": _rca_text(analysis), "version": version + 1, "previous_id": item_id,
        "root_id": data.get("root_id", item_id), "instruction": note, "constraints": constraints,
        "changes": changes, **analysis,
    }
    new_id = create_item("investigation", new_data)
    update_item(item_id, status=SUPERSEDED, superseded_by=new_id)
    issue = issues.get_issue(signal.get("issue_id"))
    if issue:
        update_item(issue["id"], investigation_id=new_id, revisions=issue["data"].get("revisions", 0) + 1)
        issue = issues.get_issue(issue["id"])
    create_item("plan_revision", {"investigation_id": item_id, "new_investigation_id": new_id,
                                  "version": version + 1, "instruction": note, "constraints": constraints,
                                  "changes": changes, "status": "applied"})
    from sre_agent.sre.incident.publish import publish_card

    publish_card(new_id, signal, signal_item["id"], analysis, new_data["rca"],
                 (issue["data"].get("count") if issue else 1) or 1, issue, version=version + 1,
                 changes=changes, instruction=note)
    step(logger, "incident.06.revise", "plan revised, new card sent", investigation_id=new_id,
         version=version + 1, changes=len(changes), previous=item_id)
    return new_id


def run_revision(item_id: str, note: str, constraints: dict) -> str | None:
    """Background wrapper: on any failure the old version is re-opened and the reviewer is told why."""
    try:
        return revise(item_id, note, constraints)
    except Exception as e:
        item = get_item(item_id)
        if item and item["status"] == REVISING:
            update_item(item_id, status=AWAITING)
        step(logger, "incident.06.revise", "revision failed, previous version re-opened", level="error",
             investigation_id=item_id, error=f"{type(e).__name__}: {e}"[:300])
        notify("plan revision failed", f"The revision of plan {item_id} could not be produced "
                                       f"({type(e).__name__}). The previous version is still open: you can "
                                       "approve, reject, or request changes again.")
        return None

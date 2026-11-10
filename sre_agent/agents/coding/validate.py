"""Pure-Python validation for Coding Agent inputs (no LLM, no I/O).

Returns blocked results instead of guessing when information is insufficient.
Path checks mirror core.git_tools.safe_path/BLOCKED_PARTS but work without
a repo root so validate() stays unit-testable offline.
"""
import os
import re
from pathlib import PurePosixPath

from sre_agent.core.config import settings
from sre_agent.core.git_tools import BLOCKED_PARTS, is_secret_path

# Files that decide what runs in CI and in production. A model-written change to one of these is a way to run
# code with secrets or to ship something unreviewed, so a human makes those changes (CODING_ALLOW_INFRA_PATHS=true
# lifts the rule).
INFRA_DIRS = {".github", ".circleci", ".gitlab", ".husky", ".buildkite", "terraform", "deploy", "deployment", "k8s",
              "helm", "ansible"}
INFRA_NAME_RE = re.compile(r"^(?:dockerfile(?:\.(?!(?:py|js|ts|rb|go|java|cs|rs|php|md)$)[\w.-]+)?|.*\.dockerfile|docker-compose.*\.ya?ml|jenkinsfile|\.gitlab-ci\.ya?ml|"
                           r"azure-pipelines\.ya?ml|bitbucket-pipelines\.ya?ml|cloudbuild\.ya?ml|.*\.tf|makefile)$", re.I)


def is_infra_path(rel: str) -> bool:
    parts = PurePosixPath(rel.strip().replace("\\", "/")).parts
    return bool(INFRA_DIRS & {p.lower() for p in parts[:-1]}) or bool(parts and INFRA_NAME_RE.match(parts[-1]))

REQUIRED_INVESTIGATION_FIELDS = (
    "root_cause",
    "remediation",
    "affected_files",
    "evidence",
    "confidence",
    "required_tests",
)


def _is_allowed_rel_path(rel: str) -> tuple[bool, str]:
    """Syntactic allowlist check (no filesystem access)."""
    if not rel or not rel.strip():
        return False, "empty path"
    if rel.strip().startswith("/") or PurePosixPath(rel).is_absolute():
        return False, "absolute paths are not allowed"
    parts = set(PurePosixPath(rel.strip()).parts)
    if ".." in parts:
        return False, "path escapes the repository"
    if BLOCKED_PARTS & parts:
        return False, "path is not allowed (.git/.env/node_modules/__pycache__)"
    if is_secret_path(rel):
        return False, "path looks like a credentials file and is never read or edited by the agent"
    if is_infra_path(rel) and os.getenv("CODING_ALLOW_INFRA_PATHS", "false").strip().lower() != "true":
        return False, ("path is a CI/deploy file (workflows, Dockerfile, Terraform, Makefile ...): a person makes "
                       "that change (CODING_ALLOW_INFRA_PATHS=true lifts this)")
    # Optional project allowlist: CODING_ALLOWED_PATHS="src/,app/" (prefix match).
    allowed = [p.strip().rstrip("/") for p in settings.coding_allowed_paths if p.strip()]
    if allowed and not any(rel.strip() == a or rel.strip().startswith(a + "/") for a in allowed):
        return False, f"outside CODING_ALLOWED_PATHS={allowed}"
    return True, ""


def validate(
    incident_id: str, signal: dict | None, investigation: dict | None
) -> tuple[bool, dict]:
    """Validate CodingInput. Returns (ok, details).

    ok=True  -> details {"clean_investigation": dict}
    ok=False -> details {"failure_reason": str, "clarification_needed": [...]}
    """
    missing: list[str] = []
    if not incident_id or not str(incident_id).strip():
        missing.append("incident_id is empty")
    if not isinstance(signal, dict) or not signal:
        missing.append("signal is missing (need monitor Signal JSON)")
    if not isinstance(investigation, dict) or not investigation:
        missing.append("investigation is missing (need InvestigationReport JSON)")
        return False, {
            "failure_reason": "; ".join(missing),
            "clarification_needed": missing,
        }

    inv = dict(investigation)
    if inv.get("fix_type") == "ops" and os.getenv("CODING_ALLOW_OPS", "false").lower() != "true":
        reason = ("investigation.fix_type is 'ops' (credentials/config/infra): no code change is recommended. "
                  "Set CODING_ALLOW_OPS=true or change fix_type to 'code' to force a code fix")
        return False, {"failure_reason": reason, "clarification_needed": [reason]}
    for field in REQUIRED_INVESTIGATION_FIELDS:
        if field not in inv or inv[field] in (None, "", [], {}):
            missing.append(f"investigation.{field} is missing/empty")

    conf = inv.get("confidence")
    try:
        conf_f = float(conf) if conf is not None else -1.0
    except (TypeError, ValueError):
        conf_f = -1.0
    if not (0.0 <= conf_f <= 1.0):
        missing.append("investigation.confidence must be 0.0-1.0")
    elif conf_f < settings.coding_min_confidence:
        missing.append(
            f"investigation.confidence {conf_f} below minimum {settings.coding_min_confidence}"
        )

    files = inv.get("affected_files") or []
    rules = inv.get("constraints") or {}  # the reviewer's limits (Request changes); enforced here as well as upstream
    deny, allow = rules.get("deny_paths") or [], rules.get("allow_paths") or []
    if isinstance(files, list):
        for f in files:
            ok, why = _is_allowed_rel_path(str(f))
            if not ok:
                missing.append(f"investigation.affected_files[{f!r}]: {why}")
            elif any(str(f) == d or str(f).startswith(d + "/") for d in deny):
                missing.append(f"investigation.affected_files[{f!r}]: the reviewer said not to touch it")
            elif allow and not any(str(f) == a or str(f).startswith(a + "/") for a in allow):
                missing.append(f"investigation.affected_files[{f!r}]: outside the folders the reviewer allowed")
    else:
        missing.append("investigation.affected_files must be a list")

    if missing:
        return False, {
            "failure_reason": "; ".join(missing),
            "clarification_needed": missing,
        }
    inv["confidence"] = conf_f
    return True, {"clean_investigation": inv}

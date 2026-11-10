"""Git + filesystem tools for agents.

Safety model (important for the demo story):
  * ANALYZER gets read-only tools on REPO_DIR. It cannot write or push.
  * FIXER gets write tools, but ONLY inside its own git worktree on its own branch.
  * Neither agent can run `git push`. Pushing is plain code triggered by a human approval.
"""
import re
import subprocess
from pathlib import Path, PurePosixPath

from langchain_core.tools import tool

from .config import settings
from .redact import redact

BLOCKED_PARTS = {".git", ".env", "node_modules", "__pycache__"}
MAX_READ_CHARS = 12_000

# Files that hold credentials, by name. The exact-name list above cannot know .env.production or id_rsa, and
# anything a read tool opens is sent to the model provider, so these are refused for reading AND writing.
SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".kube", ".docker", ".secrets", "secrets", ".password-store"}
SECRET_NAMES = {".npmrc", ".pypirc", ".netrc", ".pgpass", ".htpasswd", ".git-credentials", ".envrc", "kubeconfig",
                "wallet.dat", "terraform.tfstate", "terraform.tfstate.backup"}
TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults", ".pub")
_SECRET_RE = re.compile(r"""^(?:
      \.env(?:\..+)?                                        # .env, .env.production, .env.local
    | id_(?:rsa|dsa|ecdsa|ed25519)(?:\..*)?                  # ssh private keys
    | .*\.(?:pem|key|p12|pfx|jks|keystore|kdbx|ppk)
    | .*\.tfvars(?:\..*)?
    | service[-_]?account.*\.json
    | (?:.*[-_.])?(?:secrets?|credentials?)(?:[-_.].*)?\.(?:json|ya?ml|toml|ini|env|txt|properties|conf|cfg|xml)
    )$""", re.X | re.I)


def is_secret_path(rel) -> bool:
    """True for a path that looks like a credentials file (.env.production, id_rsa, *.pem, secrets/...).
    Template files (.env.example) are fine. Source files with 'secret' in the name (secret_rules.py) are fine."""
    parts = PurePosixPath(str(rel).replace("\\", "/")).parts
    if not parts:
        return False
    if SECRET_DIRS & {p.lower() for p in parts}:
        return True
    name = parts[-1].lower()
    if name.endswith(TEMPLATE_SUFFIXES):
        return False
    return name in SECRET_NAMES or bool(_SECRET_RE.match(name))


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run `git <args>` in `cwd` as the bot identity; return stdout+stderr.

    Raises RuntimeError on a non-zero exit unless check=False.
    """
    r = subprocess.run(
        ["git", "-c", "user.name=AI Fix Bot", "-c", "user.email=ai-fix-bot@example.com", *args],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return (r.stdout + r.stderr).strip()


def safe_path(root: Path, rel: str) -> Path:
    """Resolve `rel` inside `root`; refuse escapes and sensitive paths.

    Raises ValueError for "../" escapes, absolute paths outside root, or anything under
    BLOCKED_PARTS (.git, .env, node_modules, __pycache__). Every file tool goes through this.
    """
    p = (root / rel).resolve()
    if root.resolve() not in p.parents and p != root.resolve():
        raise ValueError("path escapes the repository")
    rel_parts = p.relative_to(root.resolve())
    if BLOCKED_PARTS & set(rel_parts.parts):
        raise ValueError("path is not allowed")
    if is_secret_path(rel_parts):
        raise ValueError("path is not allowed (looks like a credentials file)")
    return p


# ---------------- tool factories ----------------
def make_read_tools(root: Path) -> list:
    """Read-only LangChain tools bound to `root`: list_dir, read_file, git_grep, git_log."""
    @tool
    def list_dir(path: str = ".") -> str:
        """List files and folders in a directory of the repo (relative path)."""
        p = safe_path(root, path)
        return "\n".join(sorted(f"{x.name}{'/' if x.is_dir() else ''}" for x in p.iterdir()
                                if x.name not in BLOCKED_PARTS and not is_secret_path(x.name)))

    @tool
    def read_file(path: str, start_line: int = 1, end_line: int = 200) -> str:
        """Read a file (relative path) with line numbers. Use start_line/end_line to page."""
        p = safe_path(root, path)
        lines = p.read_text(errors="replace").splitlines()
        chunk = lines[max(start_line - 1, 0): end_line]
        out = "\n".join(f"{i}: {l}" for i, l in enumerate(chunk, start=max(start_line, 1)))
        return redact(out[:MAX_READ_CHARS], kv=False)  # a key hard-coded in source must not reach the model

    @tool
    def git_grep(pattern: str) -> str:
        """Search the repo for a regex/text pattern. Returns file:line:match (max 60 hits)."""
        out = _git(root, "grep", "-n", "-I", "-E", pattern, check=False)
        hits = [ln for ln in out.splitlines() if not is_secret_path(ln.split(":", 1)[0])]
        return redact("\n".join(hits[:60]), kv=False) or "no matches"

    @tool
    def git_log(path: str = ".", n: int = 8) -> str:
        """Recent commits touching a path (who changed it and when) - useful for 'what broke this'."""
        return _git(root, "log", f"-{min(n, 20)}", "--pretty=%h %an %ad %s", "--date=short", "--", path, check=False)

    return [list_dir, read_file, git_grep, git_log]


def make_write_tools(worktree: Path) -> list:
    """Write tools (write_file, run_tests) confined to one git worktree. Give these only to a fixer agent."""
    @tool
    def write_file(path: str, content: str) -> str:
        """Overwrite a file in the working tree with the FULL new content. Keep changes minimal."""
        p = safe_path(worktree, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {path} ({len(content)} chars)"

    @tool
    def run_tests() -> str:
        """Run the project's test command (if configured) and return the tail of the output."""
        if not settings.test_cmd:
            return "no TEST_CMD configured - skip tests, rely on careful reading"
        r = subprocess.run(settings.test_cmd, shell=True, cwd=worktree, capture_output=True, text=True, timeout=180)
        return f"exit={r.returncode}\n{(r.stdout + r.stderr)[-3000:]}"

    return [write_file, run_tests]


# ---------------- repo / branch lifecycle (plain code, not agent tools) ----------------
def sync_repo():
    """Best-effort `git fetch` of REPO_DIR (errors ignored).

    Never checks out or pulls: REPO_DIR may be a developer's live checkout, so its branch and
    working files stay untouched. create_worktree() starts from origin/<BASE_BRANCH> instead.
    """
    _git(settings.repo_dir, "fetch", "--all", check=False)


def _start_point() -> str:
    """origin/<BASE_BRANCH> when it exists (freshly fetched), else the local BASE_BRANCH."""
    remote = f"origin/{settings.base_branch}"
    ok = subprocess.run(["git", "rev-parse", "--verify", "--quiet", remote], cwd=settings.repo_dir,
                        capture_output=True).returncode == 0
    return remote if ok else settings.base_branch


def create_worktree(incident_id: str) -> tuple[Path, str]:
    """Create an isolated worktree on a new branch `<BRANCH_PREFIX>/<incident_id>` off BASE_BRANCH.

    Returns (worktree_path, branch_name). Raises RuntimeError if the branch already exists.
    """
    branch = f"{settings.branch_prefix}/{incident_id}"
    wt = settings.worktrees_dir / incident_id
    settings.worktrees_dir.mkdir(parents=True, exist_ok=True)
    _git(settings.repo_dir, "worktree", "add", "-b", branch, str(wt), _start_point())
    return wt, branch


def diff(worktree: Path) -> str:
    """Stage everything and return the staged diff (what a commit would contain). '' if no changes."""
    _git(worktree, "add", "-A")
    return _git(worktree, "diff", "--cached", check=False)


def commit_and_push(worktree: Path, branch: str, message: str) -> str:
    """Commit all changes; push only when DRY_RUN_PUSH=false. Call from an approval handler, never from an agent."""
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", message)
    if settings.dry_run_push:
        return f"[DRY RUN] committed on {branch}, push skipped"
    _git(worktree, "push", "-u", "origin", branch)
    return f"pushed {branch}"


def remove_worktree(worktree: Path):
    """Delete a worktree created by create_worktree (the branch itself is kept)."""
    _git(settings.repo_dir, "worktree", "remove", "--force", str(worktree), check=False)


def branch_exists(branch: str) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                          cwd=settings.repo_dir, capture_output=True).returncode == 0


def cleanup_worktree(worktree: Path | str, branch: str = "", delete_branch: bool = False) -> None:
    """Remove a worktree. The branch is deleted only when `delete_branch` (work that was never shipped).

    Never raises: cleanup must not turn a finished run into a failure."""
    try:
        _git(settings.repo_dir, "worktree", "remove", "--force", str(worktree), check=False)
        if delete_branch and branch:
            _git(settings.repo_dir, "branch", "-D", branch, check=False)
        _git(settings.repo_dir, "worktree", "prune", check=False)
    except Exception:  # noqa: BLE001
        pass


def prune_stale(max_age_days: float, keep: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Remove worktree folders under WORKTREES_DIR not touched for `max_age_days`, except names in `keep`
    (worktrees still waiting for a human decision). Branches are left alone. Returns the folders removed."""
    import time

    root = settings.worktrees_dir
    if not root.is_dir() or max_age_days <= 0:
        return []
    cutoff, removed = time.time() - max_age_days * 86400, []
    for child in root.iterdir():
        if child.is_dir() and child.name not in keep and child.stat().st_mtime < cutoff:
            cleanup_worktree(child)
            if child.exists():  # git did not know it (stale folder): remove it ourselves
                import shutil

                shutil.rmtree(child, ignore_errors=True)
            removed.append(child.name)
    return removed

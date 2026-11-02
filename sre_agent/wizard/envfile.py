"""Read and update the config file in place, keeping comments, order and file permissions.

Only the value part of `KEY=value   # comment` lines is replaced; keys missing from the file are appended.
"""
import os
import re
import shutil
import tempfile
from pathlib import Path

from dotenv import dotenv_values

APPENDED_HEADER = "# ---- Added by sre-agent setup ----"
_NEEDS_QUOTES = re.compile(r"""[\s#"'\\$`]""")


def read_values(path: Path) -> dict[str, str]:
    """Current values as python-dotenv (and therefore the app) sees them."""
    if not path.is_file():
        return {}
    return {k: (v or "") for k, v in dotenv_values(path).items()}


def render(value: str) -> str:
    """A value as it must be written so python-dotenv reads it back unchanged."""
    if value == "" or not _NEEDS_QUOTES.search(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _line_re(key: str) -> re.Pattern:
    return re.compile(
        rf"""^(?P<pre>\s*(?:export\s+)?{re.escape(key)}\s*=)"""
        r"""(?P<val>"(?:[^"\\]|\\.)*"|'[^']*'|.*?)"""
        r"""(?P<post>\s+#.*)?\s*$"""
    )


def apply(text: str, updates: dict[str, str]) -> str:
    """Return text with each key's value replaced (first occurrence) or appended at the end."""
    lines = text.splitlines()
    pending = dict(updates)
    for i, line in enumerate(lines):
        for key in list(pending):
            m = _line_re(key).match(line)
            if m:
                lines[i] = f"{m['pre']}{render(pending.pop(key))}{m['post'] or ''}"
                break
    if pending:
        if APPENDED_HEADER not in lines:
            lines += ["", APPENDED_HEADER]
        lines += [f"{k}={render(v)}" for k, v in pending.items()]
    return "\n".join(lines) + "\n"


def _copy_owner_and_mode(src_stat: os.stat_result, dst: Path) -> None:
    os.chmod(dst, src_stat.st_mode & 0o777)
    if hasattr(os, "chown"):
        try:
            os.chown(dst, src_stat.st_uid, src_stat.st_gid)
        except PermissionError:
            pass


def save(path: Path, updates: dict[str, str]) -> Path | None:
    """Write updates atomically. Returns the backup path of the previous file (None for a new file)."""
    old_text = path.read_text(encoding="utf-8") if path.is_file() else ""
    old_stat = path.stat() if path.is_file() else None
    backup = None
    if old_stat is not None:
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
        _copy_owner_and_mode(old_stat, backup)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".sre-agent-env-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(apply(old_text, updates))
        if old_stat is not None:
            _copy_owner_and_mode(old_stat, Path(tmp))
        else:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return backup

"""Read-only remote log reader over SSH (paramiko). One long-lived connection, polled, survives log rotation.

Hardening: the server's host key is verified against known_hosts (SSH_STRICT_HOST_KEY=false opts out), only the
configured key is offered (no agent / ~/.ssh probing), paths are shell-quoted, and a dropped connection is
re-opened once before the error is raised. Give the agent a dedicated read-only user for the log files.

The path may contain {date} (replaced by the remote server's YYYY-MM-DD) and shell wildcards
(errors-*.jsonl -> the newest match), so a daily-rotated log is followed without touching the .env.
"""
import os
import re
import shlex

import paramiko

from .config import settings

_GLOB_RE = re.compile(r"([*?\[\]]+)")
_RETRY_ERRORS = (paramiko.SSHException, EOFError, OSError)


def _quote_glob(path: str) -> str:
    """shlex.quote every part of the path except the wildcard characters, which the remote shell must expand."""
    parts = _GLOB_RE.split(path)
    return "".join(p if i % 2 else (shlex.quote(p) if p else "") for i, p in enumerate(parts))


class RemoteLogReader:
    """Tail a remote file over SSH by polling its size and reading only the new bytes.

        reader = RemoteLogReader()
        while True:
            new = reader.read_new()      # '' until something is appended
            ...
            time.sleep(settings.poll_seconds)
    """

    def __init__(self, path: str | None = None):
        self.template = path or settings.remote_log_path
        self.path = self.template
        self.dynamic = "{date}" in self.template or bool(_GLOB_RE.search(self.template))
        self.client: paramiko.SSHClient | None = None
        self.offset: int | None = None  # None = start from "now"
        self.inode: int | None = None

    def connect(self):
        """Open the SSH connection using SSH_* settings, verifying the host key unless SSH_STRICT_HOST_KEY=false."""
        c = paramiko.SSHClient()
        if settings.ssh_strict_host_key:
            c.load_system_host_keys()
            if os.path.exists(settings.ssh_known_hosts):
                c.load_host_keys(settings.ssh_known_hosts)
            c.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(
                settings.ssh_host, port=settings.ssh_port, username=settings.ssh_user,
                key_filename=settings.ssh_key_path, timeout=15, allow_agent=False, look_for_keys=False,
            )
        except paramiko.SSHException as e:
            if any(k in str(e).lower() for k in ("known_hosts", "known hosts", "host key")):
                raise RuntimeError(
                    f"SSH host key for {settings.ssh_host} is not trusted ({e}). Verify the fingerprint, then run: "
                    f"ssh-keyscan -p {settings.ssh_port} -H {settings.ssh_host} >> {settings.ssh_known_hosts}"
                ) from e
            raise
        self.client = c

    def _run(self, cmd: str) -> str:
        """Run a shell command remotely (connecting lazily) and return stdout. Reconnects once if the link dropped."""
        for attempt in (1, 2):
            try:
                if self.client is None:
                    self.connect()
                _, out, _ = self.client.exec_command(cmd, timeout=30)
                return out.read().decode("utf-8", errors="replace")
            except _RETRY_ERRORS:
                self.close()
                if attempt == 2:
                    raise
        return ""  # unreachable: the second failed attempt raises

    def _resolve_path(self) -> str:
        """Today's concrete file for a {date} / wildcard path (the current path when nothing matches)."""
        path = self.template
        if "{date}" in path:
            path = path.replace("{date}", self._run("date +%F").strip())
        if not _GLOB_RE.search(path):
            return path
        newest = self._run(
            f"ls -tL -- {_quote_glob(path)} 2>/dev/null | grep -v '\\.gz$' | head -n 1 | xargs -r readlink -f"
        ).strip()
        return newest or self.path

    def _stat(self) -> tuple[int | None, int]:
        """(inode, size) of the current file; (None, 0) when it does not exist."""
        fields = self._run(f"stat -c '%i %s' {shlex.quote(self.path)}").split()
        if not fields:
            return None, 0
        return (int(fields[0]) if len(fields) > 1 else None), int(fields[-1])

    def read_new(self, max_bytes: int = 200_000) -> str:
        """Return text appended to the log since the last call ('' if nothing new).

        The first call only records the current size, so old errors are ignored. If the file was replaced
        (new inode), shrank, or the dated/wildcard path now points to a new file, reading restarts from the
        beginning of it. At most `max_bytes` are returned per call.
        """
        if self.dynamic:
            current = self._resolve_path()
            if current != self.path:  # the log rolled over to a new file
                self.path, self.inode = current, None
                if self.offset is not None:
                    self.offset = 0
        inode, size = self._stat()
        if self.offset is None:  # first run: only care about errors from now on
            self.offset, self.inode = size, inode
            return ""
        if self.inode is not None and inode is not None and inode != self.inode:
            self.offset = 0  # same path, different file: rotated by rename
        self.inode = inode
        if size < self.offset:  # truncated in place
            self.offset = 0
        if size == self.offset:
            return ""
        text = self._run(f"tail -c +{self.offset + 1} {shlex.quote(self.path)} | head -c {int(max_bytes)}")
        self.offset = size
        return text

    def close(self):
        """Close the SSH connection (safe to call twice)."""
        if self.client:
            try:
                self.client.close()
            finally:
                self.client = None

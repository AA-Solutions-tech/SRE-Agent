"""Local-file tail reader for the monitor agent (MVP logger).

Exactly-once semantics: every byte is processed once, starting from the
beginning for a never-watched file. The run() layer persists the offset in
SQLite, so restarts and repeated runs never re-fire (or skip) anything.

Rotation: file shrank, or the path now points to a different file (renamed away and recreated) ->
restart from the beginning. '' means "nothing new".

NOTE: this differs deliberately from core.ssh_logs.RemoteLogReader, whose
first call ignores history (right for tailing ever-growing prod logs over
SSH). Local files are snapshots — starting from zero is what demos, cron
jobs, and tests expect. The seam (log_source.LogSource) lets each source
document its own baseline; the agent treats both identically afterwards.

Khaleel's readers must honour the same three rules (see log_source.py).
"""

import os
from pathlib import Path

DEFAULT_LOG_PATH = "./data/app.log"


class LocalFileReader:
    """Tail a local log file by polling its size, reading only new bytes."""

    def __init__(self, path: str | None = None):
        self.path = Path(
            path or os.getenv("MONITOR_LOG_PATH", DEFAULT_LOG_PATH)
        )
        self.offset: int | None = None  # None = start from "now"
        self.inode: int | None = None

    def read_new(self, max_bytes: int = 200_000) -> str:
        """Return text appended since the last call ('' if nothing new)."""
        if not self.path.exists():
            raise FileNotFoundError(f"monitor log not found: {self.path}")
        st = self.path.stat()
        size = st.st_size
        if self.offset is None:  # never watched: process everything, once
            self.offset = 0
        if self.inode and st.st_ino and st.st_ino != self.inode:  # renamed away and recreated
            self.offset = 0
        self.inode = st.st_ino
        if size < self.offset:  # truncated in place
            self.offset = 0
        if size == self.offset:
            return ""
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            raw = f.read(min(size - self.offset, max_bytes))
        self.offset += len(raw)  # a big backlog is read over several polls, none of it skipped
        return raw.decode("utf-8", errors="replace")

    def close(self) -> None:
        """No connection to close (safe to call twice, keeps the interface)."""

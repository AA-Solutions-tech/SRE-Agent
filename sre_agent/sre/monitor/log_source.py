"""Logger seam for the monitor agent.

The agent consumes ONLY this interface, so Khaleel (or anyone) can add new
log readers without touching the agent:

    class MyReader:
        def read_new(self) -> str: ...   # new text since last call ('' if none)
        def close(self) -> None: ...

Semantics every reader must honour:
  * A stable baseline — a byte is processed exactly once across restarts
    (the agent persists offsets; readers just track them in-memory).
  * Survives log rotation — if the log shrank, reading restarts from the start.
  * Never raises for "nothing new" — that is just ''.

Baseline detail is source-defined: LocalFileReader starts from the
beginning (snapshots/demos), core.ssh_logs.RemoteLogReader starts from
"now" (live prod tails). The agent treats both identically afterwards.
"""

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class LogSource(Protocol):
    """Anything the monitor can tail."""

    def read_new(self) -> str:
        """Return log text appended since the last call ('' if nothing new)."""
        ...

    def close(self) -> None:
        """Release the reader (safe to call twice)."""
        ...


def get_source(name_or_path: str | None = None) -> LogSource:
    """Return a LogSource by name or file path.

    Args:
        name_or_path: 'local_file' (default log path), 'ssh' (remote log over
            SSH via core.ssh_logs), 'docker:<container>', 'journald:<unit>',
            or any other string which is treated as a local file path. Empty/None falls back to MONITOR_SOURCE env
            (default 'local_file').
    Raises:
        ValueError: only for the reserved unknown-source case — file paths
            always resolve to LocalFileReader (missing files fail at read
            time with a clear FileNotFoundError, not here).
    """
    from .local_file import LocalFileReader

    key = (name_or_path or os.getenv("MONITOR_SOURCE", "local_file")).strip()
    if key in ("", "local_file"):
        return LocalFileReader()
    if key == "ssh":
        from sre_agent.core.ssh_logs import RemoteLogReader

        return RemoteLogReader()
    if key.startswith("docker:"):
        from .command_logs import DockerReader

        return DockerReader(key.split(":", 1)[1])
    if key.startswith("journald:"):
        from .command_logs import JournaldReader

        return JournaldReader(key.split(":", 1)[1])
    return LocalFileReader(key)  # anything else is a file path

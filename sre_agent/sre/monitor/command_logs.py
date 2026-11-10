"""Log sources that come from a command instead of a file: Docker containers and systemd journald units.

    docker:<container>      docker logs --since <last timestamp> --timestamps <container>
    journald:<unit>         journalctl -u <unit> --since @<last second> -o json

Same contract as every LogSource (see log_source.py): read_new() returns text appended since the last call.
Like the SSH reader these start from "now" (a live tail): the first call only sets the baseline, and what a
service logged while the agent was down is not replayed. Lines are de-duplicated at the boundary timestamp so
nothing is processed twice. Names are validated and passed as a list (no shell), so a name from config
cannot inject a command.
"""
import json
import re
import subprocess
import time
from datetime import datetime, timezone

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]*$")
TIMEOUT = 30


def _run(args: list[str]) -> str:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT, encoding="utf-8",
                              errors="replace")
    except FileNotFoundError as e:
        raise RuntimeError(f"{args[0]} is not installed on this host") from e
    if done.returncode != 0 and not done.stdout:
        raise RuntimeError(f"{args[0]} failed: {done.stderr.strip()[:200] or done.returncode}")
    return done.stdout + (done.stderr if args[0] == "docker" else "")  # docker puts app stderr on stderr


def _checked(name: str, what: str) -> str:
    if not NAME_RE.match(name or ""):
        raise ValueError(f"invalid {what} name: {name!r}")
    return name


class DockerReader:
    """Tail a container's logs. Lines look like '2026-11-10T06:05:24.123456789Z message'."""

    def __init__(self, container: str, runner=_run):
        self.container = _checked(container, "container")
        self._run = runner
        self._last = ""          # newest timestamp seen (RFC3339, fixed width, so it sorts as text)
        self._at_last: set[str] = set()

    def read_new(self) -> str:
        if not self._last:  # first call: baseline = now
            self._last = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"
            return ""
        out = self._run(["docker", "logs", "--timestamps", "--since", self._last, self.container])
        fresh = []
        for line in out.splitlines():
            ts, _, message = line.partition(" ")
            if not ts[:4].isdigit():
                continue  # a continuation line without a timestamp
            if ts < self._last or (ts == self._last and line in self._at_last):
                continue
            if ts > self._last:
                self._last, self._at_last = ts, set()
            self._at_last.add(line)
            fresh.append(message)
        return "\n".join(fresh) + ("\n" if fresh else "")

    def close(self) -> None:
        pass


class JournaldReader:
    """Tail a systemd unit's journal (JSON output: exact timestamps, no prefix to strip)."""

    def __init__(self, unit: str, runner=_run, clock=time.time):
        self.unit = _checked(unit, "unit")
        self._run = runner
        self._last_us = int(clock() * 1_000_000)  # baseline = now
        self._at_last: set[str] = set()

    def read_new(self) -> str:
        since = f"@{self._last_us // 1_000_000}"
        out = self._run(["journalctl", "-u", self.unit, "-o", "json", "--no-pager", "-q", "--since", since])
        fresh = []
        for line in out.splitlines():
            try:
                entry = json.loads(line)
                us, cursor, message = int(entry["__REALTIME_TIMESTAMP"]), entry.get("__CURSOR", line), entry["MESSAGE"]
            except (ValueError, KeyError, TypeError):
                continue
            if us < self._last_us or (us == self._last_us and cursor in self._at_last) or not isinstance(message, str):
                continue
            if us > self._last_us:
                self._last_us, self._at_last = us, set()
            self._at_last.add(cursor)
            fresh.append(message)
        return "\n".join(fresh) + ("\n" if fresh else "")

    def close(self) -> None:
        pass

from typing import ClassVar

from sre_agent.core import ssh_logs
from sre_agent.core.ssh_logs import RemoteLogReader


class FakeLog(RemoteLogReader):
    """Replaces the SSH transport with an in-memory file."""

    def __init__(self):
        super().__init__(path="/var/log/x.log")
        self.content = b""

    def _run(self, cmd: str) -> str:
        if cmd.startswith("stat"):
            return str(len(self.content))
        start = int(cmd.split("-c +")[1].split()[0]) - 1
        return self.content[start:].decode()


def test_first_call_ignores_existing_content():
    r = FakeLog()
    r.content = b"old error\n"
    assert r.read_new() == ""


def test_returns_only_appended_text():
    r = FakeLog()
    r.read_new()
    r.content += b"line1\n"
    assert r.read_new() == "line1\n"
    assert r.read_new() == ""
    r.content += b"line2\n"
    assert r.read_new() == "line2\n"


def test_rotation_restarts_from_beginning():
    r = FakeLog()
    r.content = b"a" * 100
    r.read_new()
    r.content = b"new\n"  # file shrank -> rotated
    assert r.read_new() == "new\n"


def test_close_is_idempotent():
    r = RemoteLogReader()
    r.close()
    assert r.client is None


class FakeClient:
    """Stands in for paramiko.SSHClient and records how it was configured."""
    seen: ClassVar[dict] = {}

    def __init__(self):
        FakeClient.seen = {"loaded": []}

    def load_system_host_keys(self):
        FakeClient.seen["loaded"].append("system")

    def load_host_keys(self, path):
        FakeClient.seen["loaded"].append(path)

    def set_missing_host_key_policy(self, p):
        FakeClient.seen["policy"] = type(p).__name__

    def connect(self, host, **kw):
        FakeClient.seen.update(host=host, **kw)


def test_connect_uses_settings_and_rejects_unknown_hosts(cfg, monkeypatch, tmp_path):
    known = tmp_path / "known_hosts"
    known.write_text("")
    cfg(ssh_host="h", ssh_port=2222, ssh_user="u", ssh_key_path="/k", ssh_known_hosts=str(known),
        ssh_strict_host_key=True)
    monkeypatch.setattr(ssh_logs.paramiko, "SSHClient", FakeClient)
    RemoteLogReader().connect()
    seen = FakeClient.seen
    assert seen["host"] == "h" and seen["port"] == 2222 and seen["username"] == "u" and seen["key_filename"] == "/k"
    assert seen["policy"] == "RejectPolicy" and str(known) in seen["loaded"]
    assert seen["allow_agent"] is False and seen["look_for_keys"] is False


def test_strict_host_key_can_be_switched_off(cfg, monkeypatch):
    cfg(ssh_host="h", ssh_strict_host_key=False)
    monkeypatch.setattr(ssh_logs.paramiko, "SSHClient", FakeClient)
    RemoteLogReader().connect()
    assert FakeClient.seen["policy"] == "AutoAddPolicy"


def test_untrusted_host_key_gives_an_actionable_error(cfg, monkeypatch):
    cfg(ssh_host="h", ssh_strict_host_key=True, ssh_known_hosts="/nope")

    class Rejecting(FakeClient):
        def connect(self, host, **kw):
            raise ssh_logs.paramiko.SSHException("Server 'h' not found in known_hosts")

    monkeypatch.setattr(ssh_logs.paramiko, "SSHClient", Rejecting)
    try:
        RemoteLogReader().connect()
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "ssh-keyscan" in str(e)


def test_path_with_spaces_and_quotes_is_shell_quoted():
    import shlex

    cmds = []

    class Spy(RemoteLogReader):
        def _run(self, cmd):
            cmds.append(cmd)
            return "5"

    path = "/var/log/my app/it's.log"
    Spy(path=path).read_new()
    assert shlex.quote(path) in cmds[0]


def test_dropped_connection_is_reopened_once():
    class Flaky(RemoteLogReader):
        connects = 0

        def connect(self):
            Flaky.connects += 1
            first = Flaky.connects == 1

            class Out:
                def read(self):
                    return b"42"

            class C:
                def exec_command(self, cmd, timeout):
                    if first:
                        raise EOFError("dropped")
                    return None, Out(), None

                def close(self):
                    pass

            self.client = C()

    r = Flaky(path="/x.log")
    assert r._run("stat x") == "42" and Flaky.connects == 2


def test_replaced_file_with_same_path_restarts_from_beginning():
    class Inode(RemoteLogReader):
        def __init__(self):
            super().__init__(path="/var/log/x.log")
            self.inode_no, self.content = 1, b""

        def _run(self, cmd):
            if cmd.startswith("stat"):
                return f"{self.inode_no} {len(self.content)}"
            start = int(cmd.split("-c +")[1].split()[0]) - 1
            return self.content[start:].decode()

    r = Inode()
    r.content = b"x" * 50
    r.read_new()
    r.inode_no, r.content = 2, b"fresh error line that is longer than the old file .........\n"
    assert r.read_new().startswith("fresh error")


def test_wildcard_path_follows_the_newest_file():
    state = {"newest": "/logs/errors-2026-10-22.jsonl", "sizes": {}}

    class Dyn(RemoteLogReader):
        def _run(self, cmd):
            if cmd.startswith("ls"):
                assert "errors-*.jsonl" in cmd and "/logs/errors-" in cmd
                return state["newest"] + "\n"
            if cmd.startswith("stat"):
                return str(state["sizes"].get(self.path, 0))
            return "NEW\n"

    r = Dyn(path="/logs/errors-*.jsonl")
    state["sizes"] = {"/logs/errors-2026-10-22.jsonl": 100}
    assert r.read_new() == "" and r.path == "/logs/errors-2026-10-22.jsonl"
    state["newest"] = "/logs/errors-2026-10-23.jsonl"
    state["sizes"]["/logs/errors-2026-10-23.jsonl"] = 4
    assert r.read_new() == "NEW\n" and r.path == "/logs/errors-2026-10-23.jsonl"


def test_date_placeholder_uses_the_remote_servers_date():
    seen = []

    class Dated(RemoteLogReader):
        def _run(self, cmd):
            seen.append(cmd)
            return "2026-10-23\n" if cmd.startswith("date") else "0"

    r = Dated(path="/logs/errors-{date}.jsonl")
    r.read_new()
    assert r.path == "/logs/errors-2026-10-23.jsonl"

"""Install/packaging support: SRE_HOME paths, API key auth, doctor, CLI, DB migrations, single-instance lock."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from sre_agent import cli, doctor, server
from sre_agent.core import db, env

client = TestClient(server.app)
KEY = "k" * 40


# ---- SRE_HOME / SRE_ENV_FILE -------------------------------------------------------------------------------

def test_home_path_defaults_under_sre_home(monkeypatch, tmp_path):
    monkeypatch.setenv("SRE_HOME", str(tmp_path))
    monkeypatch.delenv("WORKTREES_DIR", raising=False)
    assert env.sre_home() == tmp_path.resolve()
    assert env.home_path("WORKTREES_DIR", "worktrees") == tmp_path.resolve() / "worktrees"


def test_home_path_explicit_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("SRE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("REPO_DIR", str(tmp_path / "elsewhere"))
    assert env.home_path("REPO_DIR", "repo") == (tmp_path / "elsewhere").resolve()


def test_sre_home_blank_falls_back_to_data(monkeypatch):
    monkeypatch.setenv("SRE_HOME", "  ")
    assert env.sre_home().name == "data"


# ---- API key ---------------------------------------------------------------------------------------------------

def test_api_open_when_no_key_configured():
    assert client.get("/signals").status_code == 200


@pytest.mark.parametrize("path", ["/signals", "/issues", "/agents"])
def test_api_rejects_missing_key(cfg, path):
    cfg(api_key=KEY)
    r = client.get(path)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_api_rejects_wrong_key(cfg):
    cfg(api_key=KEY)
    assert client.get("/signals", headers={"X-API-Key": "nope"}).status_code == 401


def test_api_accepts_header_and_bearer(cfg):
    cfg(api_key=KEY)
    assert client.get("/signals", headers={"X-API-Key": KEY}).status_code == 200
    assert client.get("/signals", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200


def test_run_endpoint_requires_key(cfg):
    cfg(api_key=KEY)
    assert client.post("/run/hello", json={"question": "hi"}).status_code == 401


def test_health_and_approval_links_stay_open(cfg):
    cfg(api_key=KEY)
    health = client.get("/health")
    assert health.status_code == 200 and health.json()["version"]
    # approval links authenticate with their own HMAC token, not the API key: bad token -> 403, not 401
    assert client.get("/approve/x/plan/approve", params={"token": "bad"}).status_code in (403, 404)


# ---- doctor ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,ok", [
    ("", False), ("change-me", False), ("change-me-to-something-random", False), ("short", False), ("x" * 32, True),
])
def test_secret_problem(value, ok):
    assert (doctor.secret_problem(value) == "") is ok


def test_doctor_quick_fails_on_default_secret(cfg, tmp_path, capsys):
    cfg(approval_secret="change-me", data_dir=tmp_path)
    assert doctor.main(quick=True) == 1
    assert "[FAIL] APPROVAL_SECRET" in capsys.readouterr().out


def test_doctor_quick_passes_with_good_config(cfg, tmp_path, capsys):
    cfg(approval_secret="s" * 40, api_key=KEY, data_dir=tmp_path)
    assert doctor.main(quick=True, as_json=True) == 0
    checks = {c["check"]: c["status"] for c in json.loads(capsys.readouterr().out)}
    assert checks["APPROVAL_SECRET"] == checks["SRE_API_KEY"] == checks["database"] == "OK"


def test_doctor_warns_when_api_key_missing(cfg):
    cfg(api_key="")
    assert doctor.check_api_key()[0] == doctor.WARN


def test_doctor_command_check_finds_missing_binary(monkeypatch):
    monkeypatch.setenv("TEST_CMD", "definitely-not-a-real-binary-xyz --run")
    status, _, detail = doctor.check_command("TEST_CMD")
    assert status == doctor.WARN and "definitely-not-a-real-binary-xyz" in detail


def test_doctor_ssh_requires_host(cfg, monkeypatch):
    monkeypatch.setenv("MONITOR_SOURCE", "ssh")
    cfg(ssh_host="")
    assert doctor.check_ssh()[0] == doctor.FAIL


def test_doctor_full_never_raises(cfg, tmp_path):
    cfg(data_dir=tmp_path)
    names = [n for _, n, _ in doctor.run_checks(quick=False)]
    assert {"git", "TEST_CMD", "LLM key", "notifications"} <= set(names)


# ---- CLI -------------------------------------------------------------------------------------------------------

def test_cli_version_and_gen_secret(capsys):
    assert cli.main(["version"]) == 0
    assert cli.main(["gen-secret"]) == 0
    out = capsys.readouterr().out.split()
    assert out[0] == "sre-agent" and len(out[-1]) == 64


def test_cli_serve_refuses_default_secret(cfg, capsys):
    cfg(approval_secret="change-me")
    assert cli.main(["serve"]) == 2
    assert "APPROVAL_SECRET" in capsys.readouterr().err


def test_cli_serve_refuses_second_instance(cfg, tmp_path, capsys):
    cfg(approval_secret="s" * 40, data_dir=tmp_path)
    held = cli._acquire_lock(tmp_path)
    try:
        assert held is not None
        assert cli.main(["serve"]) == 3
        assert "already running" in capsys.readouterr().err
    finally:
        held.close()


def test_cli_paths(capsys):
    assert cli.main(["paths"]) == 0
    assert "SRE_HOME" in capsys.readouterr().out


# ---- DB migrations ---------------------------------------------------------------------------------------------

def test_migrate_sets_schema_version(tmp_path):
    with sqlite3.connect(tmp_path / "a.db") as c:
        assert db.migrate(c) == db.SCHEMA_VERSION
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert db.migrate(c) == db.SCHEMA_VERSION  # idempotent


def test_migrate_upgrades_pre_versioned_db(tmp_path):
    """DBs from before versioning have the table but user_version 0: migration must keep their rows."""
    with sqlite3.connect(tmp_path / "old.db") as c:
        c.executescript(db.MIGRATIONS[0])
        c.execute("INSERT INTO items VALUES ('a','k','new','{}','t','t')")
        db.migrate(c)
        assert c.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_migrate_refuses_newer_db(tmp_path):
    with sqlite3.connect(tmp_path / "new.db") as c:
        c.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
        with pytest.raises(RuntimeError, match="newer than this release"):
            db.migrate(c)

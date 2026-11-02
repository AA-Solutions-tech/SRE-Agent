"""Investigation helpers: tools, citation verification, calibration, pattern. Offline, temp git repo."""
import subprocess

import pytest

from sre_agent.sre.incident import investigate as inv

SRC = "function load(user) {\n  const doc = find(user);\n  const { id } = doc;\n  return id;\n}\n"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    (r / "src" / "load.js").write_text(SRC)
    (r / "src" / "load.test.js").write_text("load()\n")
    run = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=r, check=True,  # noqa: E731
                                    capture_output=True)
    run("init", "-q")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return r


def _tools(repo):
    read: set[str] = set()
    return {t.name: t for t in inv.make_tools(repo, read)}, read


def test_grep_skips_tests_and_read_tracks_files(repo):
    tools, read = _tools(repo)
    out = tools["grep_code"].invoke({"term": "load"})
    assert "src/load.js" in out and "load.test.js" not in out
    assert "3:" in tools["read_file"].invoke({"path": "src/load.js", "start_line": 1, "end_line": 5})
    assert read == {"src/load.js"}


def test_read_file_refuses_escape(repo):
    tools, _ = _tools(repo)
    with pytest.raises(ValueError):
        tools["read_file"].invoke({"path": "../outside.txt"})


def test_verify_citations_accepts_real_and_drops_fake(repo):
    cites = [{"path": "src/load.js", "line": 3, "quote": "const { id } = doc;"},
             {"path": "src/load.js", "line": 3, "quote": "this text is not in the file"},
             {"path": "src/missing.js", "line": 1, "quote": "x"}]
    verified, failed = inv.verify_citations(repo, cites)
    assert [c["line"] for c in verified] == [3] and failed == 2


def test_calibrate_caps_unverified_code_fix():
    assert inv.calibrate(0.9, "code", [], 0, ["a.js"]) == 0.3
    assert inv.calibrate(0.9, "code", [{"path": "a.js"}], 1, ["a.js"]) == 0.5
    assert inv.calibrate(0.9, "ops", [], 0, []) == 0.9


def test_pattern_text_reports_gap():
    rel = [{"ts": f"2026-10-19 13:{m:02d}:00:000"} for m in (10, 15, 20)]
    text = inv.pattern_text({"ts": "2026-10-19 13:25:00:000"}, rel)
    assert "4 signals" in text and "5.0 min" in text


def test_history_for_lists_commits(repo):
    rows = inv.history_for(repo, ["src/load.js"], {"src/load.js": 3})
    assert any("init" in r for r in rows) and any("last changed in" in r for r in rows)


def test_credentials_category_is_never_a_code_fix():
    from sre_agent.sre.incident.code_inspect import _clean

    raw = {"root_cause": "token revoked", "category": "credentials", "fix_type": "code", "confidence": 0.9,
           "affected_files": ["a.js"]}
    assert _clean(raw, {"a.js": 3}, {"evidence": ["x"]}, None)["fix_type"] == "ops"

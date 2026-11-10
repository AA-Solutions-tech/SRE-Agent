"""Secret files, approval links, redaction before anything leaves the machine, CI/deploy file protection. Offline."""
import json
import subprocess
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from sre_agent import server
from sre_agent.agents.coding import agent as coding
from sre_agent.agents.coding import validate as v
from sre_agent.agents.coding.prompts import patch_prompt
from sre_agent.core import approvals
from sre_agent.core import git_tools as g
from sre_agent.core import notify as n
from sre_agent.core.db import create_item, get_item
from sre_agent.sre import auto
from sre_agent.sre.incident import investigate as inv
from sre_agent.sre.monitor.parse import build_signal, classify
from sre_agent.sre.supervisor import router_clef


# ---------------- 1. credentials files are never read or edited ----------------

SECRET_FILES = [".env", ".env.production", ".env.local", "config/credentials.json", "id_rsa", "id_ed25519",
                "secrets/prod.yml", ".npmrc", "certs/server.pem", "a/b/private.key", "terraform.tfvars",
                "service-account-prod.json", "prod-secrets.yml", ".aws/credentials", "kubeconfig", ".envrc"]
NORMAL_FILES = [".env.example", ".env.sample", "core/secret_rules.py", "src/auth/credentials.py", "README.md",
                "src/app.js", "docs/secrets-policy.md", "id_rsa.pub", "package.json", "config/settings.yml",
                "tests/test_secrets.py"]


@pytest.mark.parametrize("name", SECRET_FILES)
def test_credential_files_are_detected(name):
    assert g.is_secret_path(name)


@pytest.mark.parametrize("name", NORMAL_FILES)
def test_ordinary_files_with_secret_like_words_are_not(name):
    assert not g.is_secret_path(name)


@pytest.mark.parametrize("name", [".env.production", "id_rsa", "secrets/x.yml", "config/credentials.json", ".npmrc"])
def test_safe_path_refuses_credential_files(tmp_path, name):
    with pytest.raises(ValueError):
        g.safe_path(tmp_path, name)
    assert g.safe_path(tmp_path, "src/app.js").name == "app.js"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    sh = lambda *a: subprocess.run(a, cwd=r, check=True, capture_output=True)  # noqa: E731
    sh("git", "init", "-b", "main")
    sh("git", "config", "user.email", "t@t")
    sh("git", "config", "user.name", "t")
    (r / "src").mkdir()
    (r / "src" / "app.js").write_text('const key = "sk-abcdefghijklmnopqrstuvwx";\nfunction pay() { return 1 }\n')
    (r / "config").mkdir()
    (r / "config" / "credentials.json").write_text('{"password": "hunter2-secret-value"}')
    (r / ".env.local").write_text("DATABASE_URL=postgres://u:supersecretpw@db/x\n")
    sh("git", "add", "src", "config")  # .env.local stays untracked, like a real one
    sh("git", "commit", "-m", "init")
    return r


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_investigation_tools_cannot_open_credential_files(repo):
    read = _tool(inv.make_tools(repo, set()), "read_file")
    for secret in (".env.local", "config/credentials.json"):
        with pytest.raises(ValueError):
            read.invoke({"path": secret})


def test_investigation_tools_hide_credential_hits_and_mask_keys_in_code(repo):
    tools = inv.make_tools(repo, set())
    grep = _tool(tools, "grep_code").invoke({"term": "password"})
    assert "credentials.json" not in grep and "hunter2" not in grep
    cfg = _tool(tools, "search_config").invoke({"term": "password"})
    assert "hunter2" not in cfg
    shown = _tool(tools, "read_file").invoke({"path": "src/app.js"})
    assert "sk-abcdefghijklmnopqrstuvwx" not in shown and "function pay()" in shown  # key masked, code intact


def test_kit_read_tools_have_the_same_limits(repo):
    tools = {t.name: t for t in g.make_read_tools(repo)}
    with pytest.raises(ValueError):
        tools["read_file"].invoke({"path": ".env.local"})
    assert ".env.local" not in tools["list_dir"].invoke({"path": "."})
    assert "credentials.json" not in tools["list_dir"].invoke({"path": "config"})
    assert "hunter2" not in tools["git_grep"].invoke({"pattern": "password"})


# ---------------- 9. CI / deploy files and credentials are not for the coding agent ----------------


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "Dockerfile", "docker-compose.prod.yml", "infra/main.tf",
                                  "Makefile", "app/deploy/run.sh", "Jenkinsfile", ".gitlab-ci.yml"])
def test_coding_refuses_ci_and_deploy_files(path):
    ok, why = v._is_allowed_rel_path(path)
    assert not ok and "CI/deploy" in why


def test_coding_refuses_credential_files_and_allows_normal_source(monkeypatch):
    ok, why = v._is_allowed_rel_path(".env.production")
    assert not ok and "credentials" in why
    for fine in ("src/app.js", "docs/deploy.md", "src/Makefile.js", "lib/dockerfile_parser.py"):
        assert v._is_allowed_rel_path(fine)[0], fine


def test_infra_files_can_be_allowed_on_purpose(monkeypatch):
    monkeypatch.setenv("CODING_ALLOW_INFRA_PATHS", "true")
    assert v._is_allowed_rel_path(".github/workflows/ci.yml")[0]
    assert not v._is_allowed_rel_path(".env.production")[0]  # credentials stay refused


# ---------------- 2. approval links ----------------

stage_results = []


@approvals.on_approval("sectest", extra_actions={"ignore": "Ignore"})
def _handler(item_id, action):
    stage_results.append((item_id, action))
    return f"did {action}"


def _link(item, action, **kw):
    u = urlparse(n.approval_url(item, "sectest", action))
    q = parse_qs(u.query)
    return u.path, q["token"][0], int(q["exp"][0]), u.path + "?" + u.query


@pytest.fixture
def item(cfg, monkeypatch):
    cfg(approval_secret="a-long-test-secret-0123456789", app_base_url="http://x:8000")
    monkeypatch.setenv("APPROVAL_ACCEPT_LEGACY", "false")
    stage_results.clear()
    return create_item("sectest", {"service": "api", "error": "TypeError: boom"}, status="awaiting_sectest")


def test_a_link_for_one_button_cannot_be_turned_into_another(item):
    _, reject_token, exp, _ = _link(item, "reject")
    with pytest.raises(PermissionError):
        approvals.handle_decision(item, "sectest", "approve", reject_token, exp)  # edit the URL: reject -> approve
    with pytest.raises(PermissionError):
        approvals.handle_decision(item, "sectest", "ignore", reject_token, exp)
    assert not stage_results and get_item(item)["status"] == "awaiting_sectest"


def test_links_expire_and_cannot_be_extended(item):
    _, tok, exp, _ = _link(item, "approve")
    n.verify_approval(item, "sectest", "approve", tok, exp, now=exp - 10)
    with pytest.raises(PermissionError, match="expired"):
        n.verify_approval(item, "sectest", "approve", tok, exp, now=exp + 1)
    with pytest.raises(PermissionError, match="invalid"):  # a later expiry needs a new signature
        n.verify_approval(item, "sectest", "approve", tok, exp + 10_000, now=exp + 1)
    with pytest.raises(PermissionError):
        n.verify_approval(item, "sectest", "approve", tok, "not-a-number")


def test_the_link_lifetime_is_configurable(item, monkeypatch):
    monkeypatch.setenv("APPROVAL_LINK_HOURS", "1")
    _, _, exp, _ = _link(item, "approve")
    import time

    assert 3500 < exp - time.time() <= 3600


def test_old_style_links_are_refused_unless_you_allow_them(item, monkeypatch):
    legacy = n.approval_token(item, "sectest")
    with pytest.raises(PermissionError):
        approvals.handle_decision(item, "sectest", "approve", legacy)
    monkeypatch.setenv("APPROVAL_ACCEPT_LEGACY", "true")
    assert approvals.handle_decision(item, "sectest", "approve", legacy) == "did approve"


def test_opening_a_link_decides_nothing_the_button_does(item):
    client = TestClient(server.app)
    _, _, _, url = _link(item, "approve")
    seen = client.get(url)  # what an email scanner or a chat preview does
    assert seen.status_code == 200 and "Confirm approve" in seen.text and "TypeError: boom" in seen.text
    assert get_item(item)["status"] == "awaiting_sectest" and not stage_results
    assert seen.headers["x-frame-options"] == "DENY" and seen.headers["cache-control"] == "no-store"
    assert seen.headers["referrer-policy"] == "no-referrer"
    done = client.post(url)
    assert done.status_code == 200 and get_item(item)["status"] == "sectest_approved" and stage_results == [(item, "approve")]


def test_the_decision_records_where_it_came_from(item):
    client = TestClient(server.app)
    client.post(_link(item, "ignore")[3], headers={"user-agent": "Mozilla/5.0 test"})
    who = get_item(item)["data"]["decided_from"]
    assert who["agent"] == "Mozilla/5.0 test" and who["ip"] and who["at"]


def test_bad_expired_and_repeated_links_are_refused_over_http(item):
    client = TestClient(server.app)
    path, tok, exp, url = _link(item, "approve")
    assert client.get(f"{path}?token=bad&exp={exp}").status_code == 403
    assert client.post(f"{path}?token={tok}&exp={exp + 99}").status_code == 403
    assert client.get(f"{path}?token={tok}").status_code == 403  # no expiry given: not the new format
    assert client.post(url).status_code == 200
    assert client.post(url).status_code == 409 and client.get(url).status_code == 409  # single use


def test_reminders_carry_fresh_links_and_only_while_the_card_waits(cfg, monkeypatch):
    from sre_agent.sre.incident import agent as incident  # registers the incident gate and its extra buttons

    cfg(approval_secret="a-long-test-secret-0123456789", app_base_url="http://x:8000")
    waiting = create_item("investigation", {"service": "api"}, status=f"awaiting_{incident.STAGE}")
    buttons = auto._reminder_buttons(waiting)
    assert [b[0] for b in buttons] == ["Approve", "Reject", "I'll fix this", "Ignore", "Ignore similar"]
    assert all("&exp=" in b[1] for b in buttons)
    notice = create_item("investigation", {"service": "api", "notice": True}, status=f"awaiting_{incident.STAGE}")
    assert [b[0] for b in auto._reminder_buttons(notice)] == ["I'll fix this", "Ignore", "Ignore similar"]
    answered = create_item("investigation", {"service": "api"}, status=f"{incident.STAGE}_approved")
    assert auto._reminder_buttons(answered) == [] and auto._reminder_buttons(None) == []


# ---------------- 5. nothing leaves the machine with a secret in it ----------------


def _signal_from(*lines):
    parts = classify(list(lines))
    return build_signal(parts["signal"], parts["quarantine"], "test")


def test_secrets_are_masked_before_a_signal_is_stored():
    sig = _signal_from("[2026-10-09T11:00:01Z] [ERROR] [api] login failed password=hunter2 token=abcdef1234567890",
                       "[2026-10-09T11:00:02Z] [ERROR] [api] call failed Authorization: Bearer abcdefghij123456")
    text = json.dumps(sig)
    assert "hunter2" not in text and "abcdef1234567890" not in text and "abcdefghij123456" not in text
    assert sig["service"] == "api" and "login failed" in sig["evidence"][0]  # still readable evidence


def test_the_routing_prompt_sent_to_a_third_party_is_redacted():
    prompt = router_clef.build_prompt({"service": "api", "severity": "HIGH", "error": "boom", "ts": "t",
                                      "evidence": ["upstream said Bearer abcdefghij123456 and password=hunter2"]},
                                     ["incident"])
    assert "abcdefghij123456" not in prompt and "hunter2" not in prompt and "incident" in prompt


def test_the_coding_prompt_masks_log_lines():
    prompt = patch_prompt("a.py", 1, 2, 2, "x = 1", {"log_evidence": ["failed password=hunter2 for user"]}, [])
    assert "hunter2" not in prompt and "failed" in prompt


def test_the_coding_agent_masks_keys_in_whatever_it_sends(monkeypatch):
    sent = []

    class Fake:
        def invoke(self, prompt):
            sent.append(prompt)

            class M:
                content = "{}"
            return M()

    monkeypatch.setattr(coding, "get_llm", lambda: Fake())
    coding._ask_llm("fix this: key = 'sk-abcdefghijklmnopqrstuvwx' and token = get_token()")
    assert "sk-abcdefghijklmnopqrstuvwx" not in sent[0] and "token = get_token()" in sent[0]  # code stays code

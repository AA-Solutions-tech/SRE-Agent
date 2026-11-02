"""`sre-agent setup`: config file editing, prompts, the step flow and the full run."""
from pathlib import Path

import pytest
from dotenv import dotenv_values

from sre_agent import cli
from sre_agent.wizard import envfile, steps, wizard
from sre_agent.wizard import validators as v
from sre_agent.wizard.prompts import Prompter, WizardAborted, mask


class Script:
    """Feeds scripted answers to a Prompter and records everything it printed."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.out: list[str] = []

    def read(self, prompt: str) -> str:
        self.out.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected question: {prompt!r}")
        answer = self.answers.pop(0)
        if answer is KeyboardInterrupt:
            raise KeyboardInterrupt
        return answer

    def prompter(self) -> Prompter:
        return Prompter(read=self.read, read_secret=self.read, write=self.out.append)

    def printed(self) -> str:
        return "\n".join(self.out)


# ---- envfile ---------------------------------------------------------------------------------------------------

TEMPLATE = """# header comment
SRE_API_KEY=__GENERATED__          # API key
LLM_PROVIDER=anthropic             # which provider
ANTHROPIC_API_KEY=
export TEST_CMD=npm test   # tests
"""


def test_apply_replaces_value_and_keeps_comments():
    out = envfile.apply(TEMPLATE, {"LLM_PROVIDER": "groq", "ANTHROPIC_API_KEY": "sk-1"})
    assert "LLM_PROVIDER=groq             # which provider" in out
    assert "ANTHROPIC_API_KEY=sk-1\n" in out
    assert out.startswith("# header comment\n")


def test_apply_handles_export_prefix_and_spaces_in_values():
    out = envfile.apply(TEMPLATE, {"TEST_CMD": "pytest -q tests"})
    assert 'export TEST_CMD="pytest -q tests"   # tests' in out


def test_apply_appends_unknown_keys_once():
    out = envfile.apply(TEMPLATE, {"NEW_KEY": "1"})
    out = envfile.apply(out, {"OTHER": "2"})
    assert out.count(envfile.APPENDED_HEADER) == 1
    assert out.rstrip().endswith("NEW_KEY=1\nOTHER=2")


@pytest.mark.parametrize("value", ["plain", "with space", 'quo"te', "hash#tag", "back\\slash", "dollar$X", ""])
def test_render_round_trips_through_dotenv(tmp_path, value):
    f = tmp_path / ".env"
    f.write_text(envfile.apply("K=old  # c\n", {"K": value}))
    assert (dotenv_values(f)["K"] or "") == value


def test_save_keeps_mode_and_writes_backup(tmp_path):
    f = tmp_path / "sre-agent.env"
    f.write_text(TEMPLATE)
    f.chmod(0o640)
    backup = envfile.save(f, {"LLM_PROVIDER": "openai"})
    assert backup.read_text() == TEMPLATE
    assert dotenv_values(f)["LLM_PROVIDER"] == "openai"
    assert (f.stat().st_mode & 0o777) == (backup.stat().st_mode & 0o777)


def test_save_creates_new_file(tmp_path):
    f = tmp_path / "new.env"
    assert envfile.save(f, {"A": "1"}) is None
    assert dotenv_values(f)["A"] == "1"


# ---- prompts ---------------------------------------------------------------------------------------------------

def test_required_question_repeats_until_answered():
    s = Script("", "", "value")
    assert s.prompter().text("Name", required=True) == "value"
    assert s.printed().count("This value is required.") == 2


def test_optional_question_can_be_skipped_kept_or_cleared():
    assert Script("").prompter().text("Opt") == ""
    assert Script("").prompter().text("Opt", current="keep-me") == "keep-me"
    assert Script("-").prompter().text("Opt", current="old") == ""


def test_default_is_used_on_enter():
    assert Script("").prompter().text("Port", default="22") == "22"


def test_invalid_answer_is_rejected_then_accepted():
    s = Script("abc", "70000", "2222")
    assert s.prompter().text("Port", validate=v.port) == "2222"
    assert s.printed().count("Invalid") == 2


def test_secret_current_value_is_masked_in_prompt():
    s = Script("")
    assert s.prompter().text("Key", current="sk-ant-1234567890", secret=True) == "sk-ant-1234567890"
    assert "sk-ant" not in s.printed() and "****7890" in s.printed()


def test_choice_by_number_name_and_default():
    opts = [("a", "A"), ("b", "B")]
    assert Script("2").prompter().choice("Pick", opts) == "b"
    assert Script("a").prompter().choice("Pick", opts) == "a"
    assert Script("").prompter().choice("Pick", opts, default="b") == "b"
    assert Script("9", "1").prompter().choice("Pick", opts) == "a"


def test_multi_requires_one_and_accepts_mixed():
    opts = [("slack", ""), ("gchat", ""), ("email", "")]
    assert Script("", "1,email").prompter().multi("Ch", opts, current=[]) == ["slack", "email"]
    assert Script("").prompter().multi("Ch", opts, current=["gchat"]) == ["gchat"]


def test_confirm_defaults_and_retries():
    assert Script("").prompter().confirm("Ok?", default=True) is True
    assert Script("maybe", "y").prompter().confirm("Ok?") is True


def test_ctrl_c_aborts():
    with pytest.raises(WizardAborted):
        Script(KeyboardInterrupt).prompter().text("x")


def test_mask():
    assert mask("") == "" and mask("short") == "****" and mask("abcdefghijkl") == "****ijkl"


@pytest.mark.parametrize("fn,good,bad", [
    (v.https_url, "https://hooks.slack.com/x", "http://x"),
    (v.url, "http://localhost:8000", "localhost"),
    (v.email_list, "a@x.io, b@y.io", "a@x.io, nope"),
    (v.host, "10.0.1.5", "http://host"),
    (v.absolute_path, "/var/log/a.log", "logs/a.log"),
])
def test_validators(fn, good, bad):
    assert fn(good) == "" and fn(bad) != ""


# ---- steps -----------------------------------------------------------------------------------------------------

def test_llm_step_asks_for_the_provider_key():
    out = steps.llm(Script("1", "sk-ant-xyz1234567", "").prompter(), {})
    assert out == {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-xyz1234567", "LLM_MODEL": ""}


def test_llm_step_openai_compatible_needs_url():
    out = steps.llm(Script("openai_compatible", "http://localhost:11434/v1", "", "llama3").prompter(), {})
    assert out["OPENAI_BASE_URL"] == "http://localhost:11434/v1" and out["LLM_MODEL"] == "llama3"


def test_notifications_step_slack_and_email():
    s = Script("slack,email", "https://hooks.slack.com/T/B/x", "", "", "bot@x.io", "pw", "", "ops@x.io")
    out = steps.notifications(s.prompter(), {})
    assert out["NOTIFY_CHANNELS"] == "slack,email"
    assert out["SMTP_HOST"] == "smtp.gmail.com" and out["SMTP_PORT"] == "587"
    assert out["EMAIL_FROM"] == "bot@x.io" and out["EMAIL_TO"] == "ops@x.io"


def test_logs_step_local_file():
    out = steps.logs(Script("2", "payment-api", "/var/log/app/error.log").prompter(), {})
    assert out == {"MONITOR_SOURCE": "local_file", "MONITOR_SERVICE": "payment-api",
                   "MONITOR_LOG_PATH": "/var/log/app/error.log"}


class FakeSSH:
    def __init__(self):
        self.added = []

    def generate_key(self, path):
        return "ssh-ed25519 AAAA sre-agent@test"

    def fetch_host_key(self, host, port):
        return "ssh-ed25519", "AAAAKEY", "SHA256:abc"

    def known_hosts_line(self, host, port, t, k):
        return f"[{host}]:{port} {t} {k}"

    def add_known_host(self, path, line):
        self.added.append((path, line))
        return True


def test_logs_step_ssh_creates_key_and_trusts_host_after_confirmation(tmp_path):
    ops = FakeSSH()
    key = str(tmp_path / "id_ed25519")
    s = Script("ssh", "api", "10.0.0.5", "2222", "reader", "/var/log/a.log", key, str(tmp_path / "kh"),
               "y", "y", "y")
    out = steps.logs(s.prompter(), {}, ops=ops)
    assert out["SSH_HOST"] == "10.0.0.5" and out["SSH_PORT"] == "2222"
    assert "ssh-ed25519 AAAA sre-agent@test" in s.printed()
    assert "SHA256:abc" in s.printed()
    assert ops.added == [(tmp_path / "kh", "[10.0.0.5]:2222 ssh-ed25519 AAAAKEY")]


def test_logs_step_ssh_rejected_fingerprint_is_not_saved(tmp_path):
    ops = FakeSSH()
    (tmp_path / "id").write_text("exists")
    s = Script("ssh", "api", "h", "", "u", "/l.log", str(tmp_path / "id"), str(tmp_path / "kh"), "y", "n")
    steps.logs(s.prompter(), {}, ops=ops)
    assert ops.added == []


def test_code_step_skipped_by_default():
    assert steps.code(Script("").prompter(), {}) == {}


def test_automation_turns_on_coding_with_push():
    out = steps.automation(Script("y", "y", "y", "y", "").prompter(), {})
    assert out == {"SRE_AUTO_RUN": "true", "CODING_ENABLED": "true", "DRY_RUN_PUSH": "false", "SHIP_MODE": "approve"}


# ---- whole wizard ----------------------------------------------------------------------------------------------

def _only_llm(p, cur):
    return {"LLM_PROVIDER": p.text("provider", current=cur.get("LLM_PROVIDER", ""))}


def test_run_generates_placeholder_secrets_and_saves(tmp_path):
    f = tmp_path / "sre-agent.env"
    f.write_text("SRE_API_KEY=__GENERATED__\nAPPROVAL_SECRET=change-me\nLLM_PROVIDER=anthropic\n")
    s = Script("groq", "y")
    assert wizard.run(f, prompter=s.prompter(), after_save=False, step_funcs={"llm": _only_llm}) == 0
    saved = dotenv_values(f)
    assert saved["LLM_PROVIDER"] == "groq"
    assert len(saved["SRE_API_KEY"]) == 64 and len(saved["APPROVAL_SECRET"]) == 64
    assert saved["SRE_API_KEY"] not in s.printed()  # secrets only shown masked
    assert Path(str(f) + ".bak").exists()


def test_run_nothing_changed_writes_nothing(tmp_path):
    f = tmp_path / "e.env"
    f.write_text(f"SRE_API_KEY={'a' * 64}\nAPPROVAL_SECRET={'b' * 64}\nLLM_PROVIDER=groq\n")
    s = Script("")
    assert wizard.run(f, prompter=s.prompter(), after_save=False, step_funcs={"llm": _only_llm}) == 0
    assert "Nothing changed" in s.printed() and not Path(str(f) + ".bak").exists()


def test_run_abort_saves_nothing(tmp_path):
    f = tmp_path / "e.env"
    f.write_text("LLM_PROVIDER=anthropic\n")
    s = Script(KeyboardInterrupt)
    assert wizard.run(f, prompter=s.prompter(), after_save=False, step_funcs={"llm": _only_llm}) == 130
    assert f.read_text() == "LLM_PROVIDER=anthropic\n"


def test_run_declined_save(tmp_path):
    f = tmp_path / "e.env"
    f.write_text("LLM_PROVIDER=anthropic\n")
    assert wizard.run(f, prompter=Script("groq", "n").prompter(), after_save=False,
                      step_funcs={"llm": _only_llm}) == 1
    assert f.read_text() == "LLM_PROVIDER=anthropic\n"


def test_run_rejects_unknown_section(tmp_path):
    s = Script()
    assert wizard.run(tmp_path / "e.env", only=["nope"], prompter=s.prompter(), after_save=False) == 2


def test_cli_setup_dispatches(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr("sre_agent.wizard.run", lambda path, only=None: seen.update(path=path, only=only) or 0)
    assert cli.main(["setup", "--env-file", str(tmp_path / "x.env"), "--only", "llm,logs"]) == 0
    assert seen == {"path": (tmp_path / "x.env").absolute(), "only": ["llm", "logs"]}

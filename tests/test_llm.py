import pytest

from sre_agent.core.llm import DEFAULT_MODELS, get_llm, text_of


class Msg:
    def __init__(self, content):
        self.content = content


def test_text_of_variants():
    assert text_of(Msg("hi")) == "hi"
    assert text_of(Msg([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])) == "ab"
    assert text_of("plain") == "plain"


def test_unknown_provider_raises(cfg):
    cfg(llm_provider="nope")
    with pytest.raises(ValueError):
        get_llm()


def test_anthropic_default_model(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg(llm_provider="anthropic", llm_model="")
    assert get_llm().model == DEFAULT_MODELS["anthropic"]


def test_model_override_and_openai(cfg, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    cfg(llm_provider="openai", llm_model="gpt-test")
    assert get_llm().model_name == "gpt-test"


def test_openai_compatible_uses_base_url(cfg, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg(llm_provider="openai_compatible", llm_model="llama", openai_base_url="http://localhost:11434/v1")
    assert "11434" in str(get_llm().openai_api_base)


def test_xai_points_at_x_ai(cfg, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    cfg(llm_provider="xai", llm_model="")
    assert "x.ai" in str(get_llm().openai_api_base)

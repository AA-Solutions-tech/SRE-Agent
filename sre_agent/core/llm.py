"""One place to pick the chat model. Switch provider with LLM_PROVIDER / LLM_MODEL in .env.

API keys are read by the provider SDKs from env: ANTHROPIC_API_KEY, OPENAI_API_KEY, XAI_API_KEY, GOOGLE_API_KEY.
"""
import os

from langchain_core.language_models.chat_models import BaseChatModel

from .config import settings

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-4o",
    "xai": "grok-3",
    "groq": "llama-3.3-70b-versatile",
    "gemini": "gemini-2.5-flash",
    "openai_compatible": "llama3.1",
}


def _temperature() -> dict:
    """{"temperature": x} only when LLM_TEMPERATURE is set, else {} (the provider's own default).

    Newer models reject a custom temperature with HTTP 400, and the agent used to send 0.0 on every call, so no
    analysis ever ran. Whatever a caller asks for, nothing is sent unless the operator sets LLM_TEMPERATURE."""
    raw = os.getenv("LLM_TEMPERATURE", "").strip()
    try:
        return {"temperature": float(raw)} if raw else {}
    except ValueError:
        return {}


def get_llm(temperature: float | None = None, **kwargs) -> BaseChatModel:
    """Return a LangChain chat model for LLM_PROVIDER / LLM_MODEL.

    Args:
        temperature: accepted for compatibility and ignored; set LLM_TEMPERATURE to send one (see _temperature).
        **kwargs: passed straight to the provider class (e.g. max_tokens=2000).
    Raises:
        ValueError: unknown LLM_PROVIDER.
    Provider packages are imported lazily, so you only need the one you use installed.
    """
    provider = settings.llm_provider
    model = settings.llm_model or DEFAULT_MODELS.get(provider, "")
    temp = _temperature()
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, **temp, **kwargs)
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, **temp, **kwargs)
    if provider == "xai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, **temp, base_url="https://api.x.ai/v1",
                          api_key=os.getenv("XAI_API_KEY"), **kwargs)
    if provider == "groq":
        from langchain_groq import ChatGroq  # reads GROQ_API_KEY from env
        return ChatGroq(model=model, **temp, **kwargs)
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI  # pip install langchain-google-genai
        return ChatGoogleGenerativeAI(model=model, **temp, **kwargs)
    if provider == "openai_compatible":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, **temp, base_url=settings.openai_base_url,
                          api_key=os.getenv("OPENAI_API_KEY", "not-needed"), **kwargs)
    raise ValueError(f"Unknown LLM_PROVIDER '{provider}'")


def text_of(message) -> str:
    """Message content as plain text (Anthropic can return a list of content blocks)."""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)

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


def get_llm(temperature: float = 0.0, **kwargs) -> BaseChatModel:
    """Return a LangChain chat model for LLM_PROVIDER / LLM_MODEL.

    Args:
        temperature: 0 = deterministic (good for agents/tools).
        **kwargs: passed straight to the provider class (e.g. max_tokens=2000).
    Raises:
        ValueError: unknown LLM_PROVIDER.
    Provider packages are imported lazily, so you only need the one you use installed.
    """
    provider = settings.llm_provider
    model = settings.llm_model or DEFAULT_MODELS.get(provider, "")
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, temperature=temperature, **kwargs)
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, temperature=temperature, **kwargs)
    if provider == "xai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, temperature=temperature, base_url="https://api.x.ai/v1",
                          api_key=os.getenv("XAI_API_KEY"), **kwargs)
    if provider == "groq":
        from langchain_groq import ChatGroq  # reads GROQ_API_KEY from env
        return ChatGroq(model=model, temperature=temperature, **kwargs)
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI  # pip install langchain-google-genai
        return ChatGoogleGenerativeAI(model=model, temperature=temperature, **kwargs)
    if provider == "openai_compatible":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, temperature=temperature, base_url=settings.openai_base_url,
                          api_key=os.getenv("OPENAI_API_KEY", "not-needed"), **kwargs)
    raise ValueError(f"Unknown LLM_PROVIDER '{provider}'")


def text_of(message) -> str:
    """Message content as plain text (Anthropic can return a list of content blocks)."""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)

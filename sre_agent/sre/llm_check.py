"""Is the model reachable with this key and model name? Answered at start-up and by `sre-agent doctor`.

A bad key, a wrong model name or a rejected parameter used to surface only as incident cards that quietly used a
template instead of an analysis. This makes the failure loud once, at the beginning, with the provider's reason.
LLM_SELFTEST_ON_START=false turns the start-up call off (it is one tiny request).
"""
import os
import threading

from sre_agent.core.config import settings
from sre_agent.core.sre_log import get_logger, step

logger = get_logger("llm_check")
LLM_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY",
    "groq": "GROQ_API_KEY", "gemini": "GOOGLE_API_KEY",
}


def configured() -> bool:
    """A model can be called: its key is set (a self-hosted OpenAI-compatible endpoint needs none)."""
    if settings.llm_provider == "openai_compatible":
        return bool(settings.openai_base_url)
    key = LLM_KEYS.get(settings.llm_provider)
    return bool(key and os.getenv(key))


def selftest() -> tuple[bool, str]:
    """One tiny call. (True, the reply) or (False, the provider's reason)."""
    try:
        from sre_agent.core.llm import get_llm, text_of

        return True, text_of(get_llm().invoke("Reply with the single word OK.")).strip()[:40]
    except Exception as e:  # noqa: BLE001 - any failure is the answer
        return False, f"{type(e).__name__}: {e}"[:300]


def check_and_alert() -> bool:
    ok, why = selftest()
    if ok:
        step(logger, "llm_check.01.selftest", "model reachable", provider=settings.llm_provider)
        return True
    step(logger, "llm_check.01.selftest", "model call failed", level="error", provider=settings.llm_provider,
         model=settings.llm_model or "default", error=why)
    try:
        from sre_agent.sre import health

        health.alert("llm_selftest", "The AI model cannot be reached",
                     f"{why}. Until this is fixed, incident cards use a template and say so. Check the API key "
                     f"({LLM_KEYS.get(settings.llm_provider, 'LLM_PROVIDER')}), LLM_MODEL and the provider's status.",
                     cooldown_minutes=120)
    except Exception:  # noqa: BLE001
        pass
    return False


def start_background() -> bool:
    if os.getenv("LLM_SELFTEST_ON_START", "true").strip().lower() == "false" or not configured():
        return False
    threading.Thread(target=check_and_alert, daemon=True, name="sre-llm-selftest").start()
    return True

"""Mask secrets in text before it leaves the machine (Slack / chat / email cards and LLM prompts).

Log lines are pasted into messages as evidence, so a token or password that a log line happens to contain
would otherwise be copied into a channel. Applied centrally in core.notify and to every LLM prompt that
carries log evidence. Our own signed approval/feedback links are left alone (they are not secrets in the
text of a card and masking them would break the buttons' fallback links).
"""
import re

MASK = "***"

_URL = re.compile(r"https?://\S+")
_SECRET_PARAM = re.compile(
    r"(?i)([?&](?:api[_-]?key|apikey|key|token|access[_-]?token|auth|secret|password|passwd|pwd|sig|signature|"
    r"client[_-]?secret)=)([^&\s\"'<>]+)")
_OWN_LINKS = ("/approve/", "/feedback/")

_RULES = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
     "[private key removed]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer " + MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MASK),
    (re.compile(r"\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{16,}"), MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"), MASK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), MASK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), MASK),
    (re.compile(r"(://[^\s:/@]+:)[^\s@/]+(@)"), r"\1" + MASK + r"\2"),  # user:password@host
]

# password=..., "api_key": "...", SMTP_PASSWORD: ...   (not after ? or &: query strings are handled above)
_KEY_VALUE = re.compile(
    r"""(?ix)(?<![?&\w.-])
    (?P<key>[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|
                      credentials?|authorization)[\w.-]*)
    (?P<sep>["']?\s*[:=]\s*["']?)
    (?P<val>[^\s"',;&}\]]{4,})""")


def _mask_url(m: re.Match) -> str:
    url = m.group(0)
    if any(part in url for part in _OWN_LINKS):
        return url
    return _SECRET_PARAM.sub(lambda p: p.group(1) + MASK, url)


def redact(text, kv: bool = True) -> str:
    """`text` with secrets replaced by ***. Non-strings are converted first. Never raises.

    kv=False skips the `password=...` style rule and masks only recognisable secrets (private keys, bearer
    tokens, JWTs, provider keys, user:password@ URLs). Use it for SOURCE CODE shown to a model, where
    `token = get_token()` is ordinary code and must stay readable."""
    out = text if isinstance(text, str) else str(text)
    try:
        out = _URL.sub(_mask_url, out)
        for pattern, repl in _RULES:
            out = pattern.sub(repl, out)
        if not kv:
            return out
        return _KEY_VALUE.sub(lambda m: m["key"] + m["sep"] + MASK if m["val"] != MASK else m.group(0), out)
    except Exception:  # noqa: BLE001 - redaction must never break a notification
        return out


def redact_all(items) -> list[str]:
    return [redact(i) for i in items]

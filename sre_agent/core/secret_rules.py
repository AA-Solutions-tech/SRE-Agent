"""What counts as a usable secret (APPROVAL_SECRET, SRE_API_KEY). No app imports, so the setup wizard can use it."""
import secrets

WEAK_SECRETS = {"", "change-me", "change-me-to-something-random", "changeme", "secret", "__generated__"}
MIN_SECRET_LEN = 16


def secret_problem(value: str) -> str:
    """'' if value is usable as a signing secret, else the reason it is not."""
    if value.strip().lower() in WEAK_SECRETS:
        return "still the default/empty value"
    if len(value.strip()) < MIN_SECRET_LEN:
        return f"shorter than {MIN_SECRET_LEN} characters"
    return ""


def new_secret() -> str:
    return secrets.token_hex(32)

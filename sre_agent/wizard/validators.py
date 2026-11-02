"""Answer checks for the setup wizard. Each returns "" when valid, else a short reason."""
import re
from urllib.parse import urlparse

_EMAIL = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")
_HOST = re.compile(r"^[A-Za-z0-9.\-:\[\]]+$")


def url(value: str, https_only: bool = False) -> str:
    parsed = urlparse(value)
    schemes = ("https",) if https_only else ("http", "https")
    if parsed.scheme not in schemes or not parsed.netloc:
        return f"must start with {' or '.join(s + '://' for s in schemes)} and include a host"
    return ""


def https_url(value: str) -> str:
    return url(value, https_only=True)


def port(value: str) -> str:
    return "" if value.isdigit() and 1 <= int(value) <= 65535 else "must be a number from 1 to 65535"


def host(value: str) -> str:
    return "" if _HOST.match(value) else "letters, digits, dots, dashes only (no scheme, no path)"


def email(value: str) -> str:
    return "" if _EMAIL.match(value) else "not an email address"


def email_list(value: str) -> str:
    bad = [e for e in (p.strip() for p in value.split(",")) if e and not _EMAIL.match(e)]
    return f"not an email address: {', '.join(bad)}" if bad else ""


def absolute_path(value: str) -> str:
    return "" if value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value) else "must be an absolute path"


def no_spaces(value: str) -> str:
    return "" if not re.search(r"\s", value) else "must not contain spaces"

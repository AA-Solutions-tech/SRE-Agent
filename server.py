"""Dev entry point kept for `uvicorn server:app --reload`. Installed builds use `sre-agent serve`."""
from sre_agent.server import app  # noqa: F401

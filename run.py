"""Dev shortcut kept for `python run.py <agent> [args]`. Installed builds use `sre-agent run <agent> ...`."""
import sys

from sre_agent.cli import cmd_run

if __name__ == "__main__":
    sys.exit(cmd_run(sys.argv[1:]))

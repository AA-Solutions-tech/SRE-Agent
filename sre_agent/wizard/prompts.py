"""Question helpers for the setup wizard. Input/output functions are injectable so the flow is testable.

Conventions shown to the user:
  Enter        keep the current value (or skip an optional question)
  -            clear an optional value
  Ctrl+C       abort; nothing is written
"""
import getpass
from collections.abc import Callable
from dataclasses import dataclass, field

Validator = Callable[[str], str]  # returns "" when valid, else the error message
CLEAR = "-"


class WizardAborted(Exception):
    """The user pressed Ctrl+C / Ctrl+D; nothing must be saved."""


def mask(value: str) -> str:
    if not value:
        return ""
    return "****" + value[-4:] if len(value) > 8 else "****"


@dataclass
class Prompter:
    read: Callable[[str], str] = input
    read_secret: Callable[[str], str] = field(default=getpass.getpass)
    write: Callable[[str], None] = print

    def _ask(self, prompt: str, secret: bool) -> str:
        try:
            return (self.read_secret if secret else self.read)(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise WizardAborted() from None

    def heading(self, text: str) -> None:
        self.write("")
        self.write(text)
        self.write("-" * len(text))

    def info(self, text: str) -> None:
        self.write(f"  {text}")

    def text(self, label: str, *, current: str = "", default: str = "", required: bool = False,
             secret: bool = False, help: str = "", validate: Validator | None = None) -> str:
        """Ask for one value. Returns the new value (or the current one when kept)."""
        if help:
            self.info(help)
        keep = current or default
        while True:
            if keep:
                hint = f"[{mask(keep) if secret else keep}]"
            else:
                hint = "(required)" if required else "(optional, Enter to skip)"
            answer = self._ask(f"  {label} {hint}: ", secret)
            if answer == CLEAR and not required:
                return ""
            if answer == "":
                if keep:
                    return keep
                if required:
                    self.info("This value is required.")
                    continue
                return ""
            error = validate(answer) if validate else ""
            if error:
                self.info(f"Invalid: {error}")
                continue
            return answer

    def choice(self, label: str, options: list[tuple[str, str]], *, current: str = "", default: str = "") -> str:
        """Pick one of (value, description). Accepts the number or the value itself."""
        values = [v for v, _ in options]
        keep = current if current in values else default
        for i, (value, desc) in enumerate(options, 1):
            marker = " (current)" if value == current else ""
            self.info(f"{i}) {value:<18} {desc}{marker}")
        while True:
            answer = self._ask(f"  {label} [{keep or 'choose a number'}]: ", False)
            if answer == "" and keep:
                return keep
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return values[int(answer) - 1]
            if answer in values:
                return answer
            self.info(f"Enter a number from 1 to {len(options)}.")

    def multi(self, label: str, options: list[tuple[str, str]], *, current: list[str],
              required: bool = True) -> list[str]:
        """Pick several, comma separated (numbers or values)."""
        values = [v for v, _ in options]
        keep = [c for c in current if c in values]
        for i, (value, desc) in enumerate(options, 1):
            self.info(f"{i}) {value:<18} {desc}")
        while True:
            answer = self._ask(f"  {label} [{','.join(keep) or 'e.g. 1,3'}]: ", False)
            if answer == "" and keep:
                return keep
            picked, bad = [], []
            for part in filter(None, (p.strip() for p in answer.split(","))):
                if part.isdigit() and 1 <= int(part) <= len(options):
                    part = values[int(part) - 1]
                (picked if part in values else bad).append(part)
            if bad:
                self.info(f"Unknown: {', '.join(bad)}")
            elif picked or not required:
                return list(dict.fromkeys(picked))
            else:
                self.info("Pick at least one.")

    def confirm(self, label: str, default: bool = False) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self._ask(f"  {label} [{hint}]: ", False).lower()
            if answer == "":
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.info("Answer y or n.")

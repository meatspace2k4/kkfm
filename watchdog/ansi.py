import re


RESET = "\x1b[0m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
DIM = "\x1b[2m"


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def strip_ansi(s: str) -> str:
    """Remove all ANSI escape sequences."""
    return _ANSI_RE.sub("", s)


def is_tty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _wrap(color: str, s: str) -> str:
    return f"{color}{s}{RESET}"


def fmt_ok(s: str) -> str:
    return _wrap(GREEN, s)


def fmt_fail(s: str) -> str:
    return _wrap(RED, s)


def fmt_skip(s: str) -> str:
    return _wrap(YELLOW, s)



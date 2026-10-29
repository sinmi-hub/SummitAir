"""Small stderr logging helpers used by the preserved Google adapters."""
from __future__ import annotations

import sys


def debug(msg: str) -> None:
    if _enabled():
        print(msg, file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _enabled() -> bool:
    from config import settings
    return settings.debug

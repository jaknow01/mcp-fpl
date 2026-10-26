"""Small env helpers and the server's league list."""

import os
from typing import Optional

_FALSY = {"0", "false", "no", "off", "disabled"}


def env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    raw = os.getenv(name)
    return raw.strip() if raw is not None and raw.strip() else default


def env_flag(name: str, default: bool = True) -> bool:
    raw = env_str(name)
    return default if raw is None else raw.lower() not in _FALSY


def env_int(name: str, default: int, minimum: Optional[int] = None) -> int:
    try:
        value = int(env_str(name) or default)
    except ValueError:
        value = default
    return max(minimum, value) if minimum is not None else value


def _id_list(name: str) -> list[int]:
    raw = env_str(name) or ""
    return [int(x) for x in raw.replace(" ", "").split(",") if x.isdigit()]


def league_ids() -> list[int]:
    """``FPL_LEAGUES``: the classic leagues this server collects history for, keeps warm,
    and whose managers may get a key on /join."""
    return _id_list("FPL_LEAGUES")


def draft_league_ids() -> list[int]:
    """``FPL_DRAFT_LEAGUES``: the FPL Draft leagues whose managers may get a key on /join
    (Draft ids are a separate id space from classic ones)."""
    return _id_list("FPL_DRAFT_LEAGUES")

"""Who "my squad" / "my league" refers to.

1. an explicit ``entry_id`` / ``league_id`` argument on the tool call;
2. the defaults linked to the caller's API key (``fpl: {entry_id, league_id}`` in the
   clients file, or the entry/league a /mcp/join key was issued for).

There is no server-wide fallback: a caller without linked defaults gets an error asking
for the id, never another manager's team.
"""

from typing import Optional

from jarvis_mcp_kit import current_client


def _linked(field: str) -> Optional[int]:
    client = current_client()
    if client is None:
        return None
    value = client.server_settings("fpl").get(field)
    return int(value) if value is not None and str(value).strip().isdigit() else None


def resolve_entry_id(override: Optional[int]) -> Optional[int]:
    return int(override) if override is not None else _linked("entry_id")


def resolve_league_id(override: Optional[int]) -> Optional[int]:
    return int(override) if override is not None else _linked("league_id")


def missing_entry_tip() -> str:
    return ("No default FPL manager id is linked to this caller. Pass entry_id explicitly "
            "(the number in the FPL site URL: fantasy.premierleague.com/entry/<id>/...), "
            "or get a personal key on the server's /mcp/join page.")


def missing_league_tip() -> str:
    return ("No default league is linked to this caller. Pass league_id explicitly, or call "
            "get_my_fpl_leagues to find your league ids.")

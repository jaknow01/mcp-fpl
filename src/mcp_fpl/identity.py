"""Who "my squad" / "my league" refers to, and in which game.

1. an explicit ``entry_id`` / ``league_id`` (and ``game``) argument on the tool call;
2. the defaults linked to the caller's API key: ``fpl: {entry_id, league_id}`` for classic
   FPL and ``fpl: {draft_entry_id, draft_league_id}`` for FPL Draft, from the clients file
   or the team(s) a /join key was issued for.

There is no server-wide fallback: a caller without linked defaults gets an error asking
for the id, never another manager's team.

Classic and Draft are separate games with separate id spaces, so an id alone never says
which game it belongs to. ``resolve_game`` decides: the explicit ``game`` argument, else
the one game the key is linked to; a key linked to both needs ``game`` spelled out.
"""

from typing import Literal, Optional, Union

from jarvis_mcp_kit import current_client

Game = Literal["classic", "draft"]


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


def resolve_draft_entry_id(override: Optional[int]) -> Optional[int]:
    return int(override) if override is not None else _linked("draft_entry_id")


def resolve_draft_league_id(override: Optional[int]) -> Optional[int]:
    return int(override) if override is not None else _linked("draft_league_id")


def linked_games() -> list[str]:
    """The games the caller's key has a team or league linked for."""
    games = []
    if _linked("entry_id") is not None or _linked("league_id") is not None:
        games.append("classic")
    if _linked("draft_entry_id") is not None or _linked("draft_league_id") is not None:
        games.append("draft")
    return games


def resolve_game(game: Optional[str]) -> Union[str, dict]:
    """"classic" or "draft" — or an error dict when the key has both games and the call
    did not say which. Without any linked game the default is classic (explicit ids from
    callers like Jarvis keep their old meaning)."""
    if game in ("classic", "draft"):
        return game
    linked = linked_games()
    if len(linked) == 1:
        return linked[0]
    if not linked:
        return "classic"
    return {
        "Error": "This key is linked to both a classic FPL team and an FPL Draft team.",
        "Tip": "Call again with game='classic' or game='draft'. If the user's question does not "
               "make it clear (draft words: waivers, free agents, trades, the draft), ask which one.",
    }


def missing_entry_tip(game: str = "classic") -> str:
    if game == "draft":
        return ("No default FPL Draft team is linked to this caller. Pass entry_id explicitly "
                "(the number after /entry/ in the draft site's team URL), or get a personal key "
                "on the server's /join page.")
    return ("No default FPL manager id is linked to this caller. Pass entry_id explicitly "
            "(the number in the FPL site URL: fantasy.premierleague.com/entry/<id>/...), "
            "or get a personal key on the server's /join page.")


def missing_league_tip(game: str = "classic") -> str:
    if game == "draft":
        return ("No default FPL Draft league is linked to this caller. Pass league_id explicitly, "
                "or call get_my_fpl_leagues with game='draft' to find the league ids.")
    return ("No default league is linked to this caller. Pass league_id explicitly, or call "
            "get_my_fpl_leagues to find your league ids.")

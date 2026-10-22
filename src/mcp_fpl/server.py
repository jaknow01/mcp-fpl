"""The `fpl` MCP server instance and its tool guide."""

from jarvis_mcp_kit import ClientRegistry, ToolServer

INSTRUCTIONS = (
    "Fantasy Premier League (FPL) data from the live FPL API. Answer from these tools, never "
    "from built-in knowledge (it is out of date). 'The owner' in tool descriptions means the "
    "person using this API key: their default manager id and league are linked to the key "
    "(see whoami); pass entry_id / league_id explicitly to look at someone else's team or "
    "another league.\n"
    "Tool guide:\n"
    "- get_fpl_fixtures: the schedule — matches for a gameweek with FDR difficulty (1=easy..5=hard) "
    "and kickoff times; defaults to the round in play. For planning, not live scores.\n"
    "- get_fpl_live: what is happening RIGHT NOW — live scores and minute, goals/assists/cards, and "
    "how the owner's players are doing (minutes, points, whether they actually started). If "
    "any_live is false, nothing is being played. my_players.starters_not_playing lists players in "
    "the owner's FPL XI who are on the real bench or did not come on — warn about them.\n"
    "- get_pl_teams: Premier League teams and strength ratings.\n"
    "- get_fpl_player: any single player by name — price, position, team, points, form, ownership, "
    "availability and next fixtures. If ambiguous:true, ask which player. Availability is injury/"
    "suspension status, NOT a confirmed lineup.\n"
    "- get_my_fpl_squad: the owner's picked squad for a gameweek with captaincy, bench, points, value.\n"
    "- get_my_fpl_leagues: the owner's mini-leagues and their ids.\n"
    "- get_fpl_league_standings: a classic mini-league table; the owner's row has is_me.\n"
    "- get_league_history: how standings or squads in a league changed over days/gameweeks.\n"
    "- who_owns_player_in_league: which managers in a mini-league own a player.\n"
    "- get_league_ownership: most-owned / most-captained players across a mini-league.\n"
    "If a tool says no id is configured, pass that on plainly — never invent an id. Run "
    "independent tools in parallel. Report ids, points and prices exactly as returned."
)


def _describe(client) -> dict:
    cfg = client.server_settings("fpl")
    return {"default_entry_id": cfg.get("entry_id"), "default_league_id": cfg.get("league_id")}


def _issued_clients():
    from mcp_fpl.keys import issued_clients  # lazy: storage is selected after env is loaded

    return issued_clients()


server = ToolServer("fpl", title="Jarvis FPL", instructions=INSTRUCTIONS,
                    registry=ClientRegistry(issued=_issued_clients), describe_client=_describe)

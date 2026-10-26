"""The `fpl` MCP server instance and its tool guide."""

from jarvis_mcp_kit import ClientRegistry, ToolServer

INSTRUCTIONS = (
    "Fantasy Premier League (FPL) data from the live FPL API, for both games: classic FPL and "
    "FPL Draft. Answer from these tools, never from built-in knowledge (it is out of date). "
    "'The owner' in tool descriptions means the person using this API key: their classic team "
    "and league and/or their Draft team and league are linked to the key (see whoami); pass "
    "entry_id / league_id explicitly to look at someone else's team or another league.\n"
    "Classic vs Draft: they are separate games with separate ids. Tools that work for both take "
    "game='classic'|'draft'; leave it out when the key is linked to one game only. When the key "
    "has both and the question does not say (draft words: waivers, free agents, trades, the "
    "draft, H2H), ask the user which game they mean. Draft has no prices, captains or chips, "
    "and each player belongs to at most one manager in a draft league.\n"
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
    "- get_my_fpl_squad: the owner's picked squad for a gameweek with captaincy, bench, points, value "
    "(Draft: starting XI + bench and points; no captain or value).\n"
    "- get_my_fpl_leagues: the owner's mini-leagues (classic and/or Draft) and their ids.\n"
    "- get_fpl_league_standings: a mini-league table; the owner's row has is_me (Draft H2H leagues "
    "also return the gameweek's head-to-head matches — 'who do I play this week').\n"
    "- get_league_history: how standings or squads in a classic league changed over days/gameweeks.\n"
    "- who_owns_player_in_league: which managers in a mini-league own a player (Draft: the one "
    "owner, or that the player is free).\n"
    "- get_league_ownership: most-owned / most-captained players across a classic mini-league.\n"
    "- get_draft_free_agents: Draft only — the best unowned players in the league (waiver / "
    "free-agent targets), filterable by position.\n"
    "- get_draft_activity: Draft only — waiver and free-agent moves, trades, and the draft picks.\n"
    "If a tool says no id is configured, pass that on plainly — never invent an id. Run "
    "independent tools in parallel. Report ids, points and prices exactly as returned."
)


def _describe(client) -> dict:
    cfg = client.server_settings("fpl")
    out = {"default_entry_id": cfg.get("entry_id"), "default_league_id": cfg.get("league_id")}
    if cfg.get("draft_entry_id") is not None or cfg.get("draft_league_id") is not None:
        out["default_draft_entry_id"] = cfg.get("draft_entry_id")
        out["default_draft_league_id"] = cfg.get("draft_league_id")
    return out


def _issued_clients():
    from mcp_fpl.keys import issued_clients  # lazy: storage is selected after env is loaded

    return issued_clients()


class _LoggedToolServer(ToolServer):
    """Every tool registered here is wrapped so its calls land in the tool-call log."""

    def add_tool(self, fn, *, name=None, **kwargs) -> None:
        from mcp_fpl.call_log import recorded

        super().add_tool(recorded(fn, name or fn.__name__), name=name or fn.__name__, **kwargs)


server = _LoggedToolServer("fpl", title="FPL", instructions=INSTRUCTIONS,
                           registry=ClientRegistry(issued=_issued_clients), describe_client=_describe)

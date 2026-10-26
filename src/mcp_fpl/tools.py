"""Fantasy Premier League tools: fixtures/FDR, PL teams, players, the caller's squad,
mini-league standings/ownership/history and live in-play data — all from the keyless public
FPL API — for classic FPL and FPL Draft (``game="classic"|"draft"``, see identity.py).

Numeric team/player/gameweek ids are resolved to human-readable names server-side (via
the bootstrap-static reference data in fpl_api), so the model — and the user — only ever
deal with names, not raw ids."""

import asyncio
import logging
from datetime import date, timedelta
from typing import Literal, Optional

from mcp_fpl.server import server
from mcp_fpl.identity import (
    linked_games,
    resolve_draft_entry_id,
    resolve_draft_league_id,
    resolve_entry_id,
    resolve_game,
    resolve_league_id,
    missing_entry_tip,
    missing_league_tip,
)
from mcp_fpl.draft_api import (
    TRADE_STATES,
    TRANSACTION_KINDS,
    TRANSACTION_RESULTS,
    DraftLeague,
    classic_ids_by_draft_id,
    describe_draft_player,
    draft_gameweek,
    fetch_draft,
    fetch_draft_bootstrap,
    fetch_draft_element_status,
    fetch_draft_game,
    fetch_draft_league,
    index_draft_bootstrap,
)
from mcp_fpl.fpl_api import (
    fetch_fpl,
    fetch_fpl_bootstrap,
    index_bootstrap,
    resolve_gameweek,
    relevant_gameweek,
    describe_player,
    summarize_fixture_events,
    find_players_by_name,
    load_league_squads,
    load_league_history,
    fpl_error,
    _LEAGUE_SCAN_DEFAULT,
)

logger = logging.getLogger(__name__)

# "The owner" in the tool descriptions = the caller: an explicit tool argument, else the
# defaults linked to the caller's API key. See identity.py.

def _fpl_entry_id(override: Optional[int]) -> Optional[int]:
    return resolve_entry_id(override)


def _fpl_league_id(override: Optional[int]) -> Optional[int]:
    return resolve_league_id(override)


Game = Optional[Literal["classic", "draft"]]


def _no_draft_entry() -> dict:
    return {"Error": "No FPL Draft team id is configured.", "Tip": missing_entry_tip("draft")}


def _no_draft_league() -> dict:
    return {"Error": "No FPL Draft league id is configured.", "Tip": missing_league_tip("draft")}


def _draft_only_caller(league_id: Optional[int]) -> bool:
    """True when the caller only plays Draft and named no league; used by classic-only tools."""
    return league_id is None and linked_games() == ["draft"]


def _live_points(live: dict) -> dict:
    """Draft ``event/{gw}/live``: element id -> that gameweek's stats (keys are strings)."""
    return {int(k): (v or {}).get("stats", {}) for k, v in (live.get("elements") or {}).items()}


@server.tool()
async def get_fpl_fixtures(gameweek: Optional[int] = None) -> dict:
    """
    Description:
        Lists the Premier League fixtures (matches) for a Fantasy Premier League
        gameweek, with each team's official FDR difficulty rating (1 = easiest,
        5 = hardest) and kickoff time. Use this to answer "what are today's / the
        upcoming matches / this round" questions.

    Parameters:
    gameweek: Optional[int] = None
        The gameweek number to list fixtures for. If omitted, the round currently in
        play is used: the current gameweek while it is still running (which is the one
        that contains "today's" matches), or the next gameweek once the current one has
        finished. To pinpoint a specific day, read each fixture's kickoff_time and
        compare it to today's date from the environment context.

    Output:
        JSON object with the resolved gameweek and a list of fixtures. Each fixture
        gives the home/away team names, their difficulty ratings, kickoff time, and —
        if the match has been played — the final score.
    """
    logger.info(f"Getting FPL fixtures for gameweek={gameweek or 'in-play'}")
    bootstrap, gw = None, None  # referenced by the error hint if the try fails early
    try:
        bootstrap = await fetch_fpl_bootstrap()
        gw = gameweek if gameweek is not None else relevant_gameweek(bootstrap)
        if gw is None:
            return {"Error": "Could not determine a gameweek (no events in the FPL calendar)."}
        teams_by_id, _, _ = index_bootstrap(bootstrap)
        fixtures = await fetch_fpl(f"fixtures/?event={gw}")
    except Exception as e:
        return fpl_error("Error while getting FPL fixtures", e,
                         bootstrap=bootstrap, gameweek=gw)

    matches = []
    for f in fixtures:
        home = teams_by_id.get(f.get("team_h"), {})
        away = teams_by_id.get(f.get("team_a"), {})
        match = {
            "home": home.get("name"),
            "away": away.get("name"),
            "home_difficulty": f.get("team_h_difficulty"),
            "away_difficulty": f.get("team_a_difficulty"),
            "kickoff_time": f.get("kickoff_time"),
            "finished": f.get("finished"),
        }
        if f.get("finished") or f.get("started"):
            match["score"] = f"{f.get('team_h_score')}-{f.get('team_a_score')}"
        matches.append(match)

    return {"gameweek": gw, "fixtures_count": len(matches), "fixtures": matches}


@server.tool()
async def get_pl_teams() -> dict:
    """
    Description:
        Lists all Premier League teams in the current Fantasy Premier League season,
        with their short codes and FPL strength ratings (overall/attack/defence,
        split home/away, on a 1-5 scale). Use this for questions about the teams
        themselves (who is in the league, relative strength).

    Parameters:
    Output:
        JSON object with a list of teams (name, short_name, and strength ratings).
    """
    logger.info("Getting Premier League teams")
    try:
        bootstrap = await fetch_fpl_bootstrap()
    except Exception as e:
        return fpl_error("Error while getting Premier League teams", e)

    teams = [
        {
            "name": t.get("name"),
            "short_name": t.get("short_name"),
            "strength_overall_home": t.get("strength_overall_home"),
            "strength_overall_away": t.get("strength_overall_away"),
            "strength_attack_home": t.get("strength_attack_home"),
            "strength_attack_away": t.get("strength_attack_away"),
            "strength_defence_home": t.get("strength_defence_home"),
            "strength_defence_away": t.get("strength_defence_away"),
        }
        for t in bootstrap.get("teams", [])
    ]
    return {"teams_count": len(teams), "teams": teams}


@server.tool()
async def get_fpl_player(name: str, upcoming_fixtures: int = 5) -> dict:
    """
    Description:
        Looks up a single Premier League player by name and returns their FPL profile:
        current price, position, team, total points, form, ownership (% selected),
        availability status, and the next few fixtures with their FDR difficulty. Use
        this for questions about ANY player (not just the owner's squad), e.g. "how much
        does Haaland cost now?", "what's Saka's form?", "who does Palmer play next and
        how hard are the fixtures?".

    Parameters:
    name: str
        Free-text player name to resolve (e.g. 'Haaland', 'Bruno Fernandes', 'Saka').
        Matching is by web name first (an exact web-name match wins); otherwise all
        substring matches across first/second/web name are returned for disambiguation.

    upcoming_fixtures: int = 5
        How many of the player's next fixtures to include (each with opponent,
        home/away, FDR difficulty 1-5 and kickoff time). Set to 0 to skip fixtures.

    Output:
        JSON object. On a single match: the player's profile plus `upcoming_fixtures`
        and `chance_of_playing_this_round`/`chance_of_playing_next_round`. When the name
        is ambiguous, returns `ambiguous: true` with a compact list of candidates (name,
        team, position, price) so the caller can pick one. Note: `status`/`chance_of_playing`
        report AVAILABILITY (injury/suspension), NOT a confirmed pre-match starting lineup —
        the FPL API does not publish announced XIs.
    """
    logger.info(f"Getting FPL player info for query={name!r}")
    try:
        bootstrap = await fetch_fpl_bootstrap()
        teams_by_id, _, positions_by_id = index_bootstrap(bootstrap)
        candidates = find_players_by_name(bootstrap, name)
    except Exception as e:
        return fpl_error("Error while getting the FPL player", e)

    if not candidates:
        return {"Error": f"No Premier League player matches '{name}'.", "query": name}

    if len(candidates) > 1:
        return {
            "query": name,
            "ambiguous": True,
            "matches_count": len(candidates),
            "matches": [describe_player(e, teams_by_id, positions_by_id) for e in candidates[:15]],
            "Tip": "Several players matched; ask the user which one or pass a more specific name.",
        }

    element = candidates[0]
    info = describe_player(element, teams_by_id, positions_by_id)
    # Availability for the upcoming round(s) — an injury/suspension signal, NOT a
    # confirmed team sheet. The FPL API has no announced-lineup (starting XI vs bench) data.
    info["chance_of_playing_this_round"] = element.get("chance_of_playing_this_round")
    info["chance_of_playing_next_round"] = element.get("chance_of_playing_next_round")

    fixtures_out = []
    if upcoming_fixtures and upcoming_fixtures > 0:
        try:
            summary = await fetch_fpl(f"element-summary/{element.get('id')}/")
            for fx in (summary.get("fixtures") or [])[:upcoming_fixtures]:
                is_home = fx.get("is_home")
                opp = teams_by_id.get(fx.get("team_a") if is_home else fx.get("team_h"), {})
                fixtures_out.append({
                    "gameweek": fx.get("event"),
                    "opponent": opp.get("name"),
                    "opponent_short": opp.get("short_name"),
                    "home_or_away": "H" if is_home else "A",
                    "difficulty": fx.get("difficulty"),
                    "kickoff_time": fx.get("kickoff_time"),
                })
        except Exception:
            logger.warning("Could not fetch upcoming fixtures for player", exc_info=True)
    info["upcoming_fixtures"] = fixtures_out

    return info


@server.tool()
async def get_my_fpl_squad(gameweek: Optional[int] = None,
                           entry_id: Optional[int] = None,
                           game: Game = None) -> dict:
    """
    Description:
        Returns the owner's Fantasy Premier League squad (the 15 players picked) for a
        gameweek, with each player resolved to name/team/position, the captain and
        vice-captain marked, and the starting XI vs. bench split. Also reports that
        gameweek's points, squad value and money in the bank. In FPL Draft there is no
        captain, value or bank: it returns the XI + bench with each player's points for the
        gameweek and the automatic substitutions made.

    Parameters:
    gameweek: Optional[int] = None
        The gameweek to fetch the squad for. If omitted, the current gameweek is used.
        Note: a squad only becomes public after that gameweek's deadline has passed.

    entry_id: Optional[int] = None
        The manager ("entry") id to look up — a classic or a Draft id depending on `game`.
        If omitted, the owner's configured default team for that game is used.

    game: "classic" | "draft" | None = None
        Which game: classic FPL or FPL Draft (separate games with separate ids). Leave it
        out when the owner's key is linked to one game only; the tool then uses that game.

    Output:
        JSON object with the resolved gameweek, a per-gameweek summary (points, bank,
        squad value) and the list of picked players (starting XI first, then bench),
        each flagged with captaincy and whether they are on the bench.
    """
    which = resolve_game(game)
    if isinstance(which, dict):
        return which
    if which == "draft":
        return await _draft_squad(gameweek, entry_id)
    eid = _fpl_entry_id(entry_id)
    if eid is None:
        return {
            "Error": "No FPL manager id is configured.",
            "Tip": missing_entry_tip(),
        }
    logger.info(f"Getting FPL squad for entry={eid}, gameweek={gameweek or 'current'}")
    bootstrap, gw = None, None  # referenced by the error hint if the try fails early
    try:
        bootstrap = await fetch_fpl_bootstrap()
        gw = resolve_gameweek(bootstrap, gameweek, prefer="current")
        if gw is None:
            return {"Error": "Could not determine a gameweek (no events in the FPL calendar)."}
        teams_by_id, elements_by_id, positions_by_id = index_bootstrap(bootstrap)
        picks_data = await fetch_fpl(f"entry/{eid}/event/{gw}/picks/")
    except Exception as e:
        return fpl_error("Error while getting the FPL squad", e,
                         bootstrap=bootstrap, gameweek=gw)

    if isinstance(picks_data, dict) and picks_data.get("detail"):
        return {"Error": f"FPL API: {picks_data['detail']}", "entry_id": eid, "gameweek": gw}

    history = picks_data.get("entry_history", {}) or {}
    players = []
    for p in picks_data.get("picks", []):
        element = elements_by_id.get(p.get("element"), {})
        info = describe_player(element, teams_by_id, positions_by_id)
        # positions 1-11 are the starting XI, 12-15 the bench (in bench order)
        info["on_bench"] = p.get("position", 0) > 11
        info["is_captain"] = p.get("is_captain", False)
        info["is_vice_captain"] = p.get("is_vice_captain", False)
        info["multiplier"] = p.get("multiplier")
        players.append(info)

    return {
        "entry_id": eid,
        "gameweek": gw,
        "summary": {
            "gameweek_points": history.get("points"),
            "total_points": history.get("total_points"),
            "overall_rank": history.get("overall_rank"),
            "bank": round(history.get("bank", 0) / 10, 1),          # £m
            "squad_value": round(history.get("value", 0) / 10, 1),  # £m
            "transfers_made": history.get("event_transfers"),
        },
        "squad": players,
    }


async def _draft_squad(gameweek: Optional[int], entry_id: Optional[int]) -> dict:
    eid = resolve_draft_entry_id(entry_id)
    if eid is None:
        return _no_draft_entry()
    logger.info(f"Getting FPL Draft squad for entry={eid}, gameweek={gameweek or 'current'}")
    gw = None
    try:
        bootstrap = await fetch_draft_bootstrap()
        gw = draft_gameweek(await fetch_draft_game(), gameweek)
        if gw is None:
            return {"Error": "Could not determine a gameweek (no current Draft gameweek)."}
        teams_by_id, elements_by_id, positions_by_id = index_draft_bootstrap(bootstrap)
        picks_data, live, history = await asyncio.gather(
            fetch_draft(f"entry/{eid}/event/{gw}"), fetch_draft(f"event/{gw}/live"),
            fetch_draft(f"entry/{eid}/history"))
    except Exception as e:
        return fpl_error("Error while getting the FPL Draft squad", e, gameweek=gw)

    stats = _live_points(live)
    players = []
    for p in picks_data.get("picks", []):
        info = describe_draft_player(elements_by_id.get(p.get("element"), {}), teams_by_id, positions_by_id)
        info["on_bench"] = p.get("position", 0) > 11  # positions 1-11 = XI, 12-15 = bench in order
        info["gameweek_points"] = stats.get(p.get("element"), {}).get("total_points")
        players.append(info)
    name = lambda el: elements_by_id.get(el, {}).get("web_name")  # noqa: E731
    subs = [{"in": name(x.get("element_in")), "out": name(x.get("element_out"))}
            for x in picks_data.get("subs", []) or []]
    row = next((h for h in history.get("history", []) if h.get("event") == gw), {})
    return {
        "game": "draft",
        "entry_id": eid,
        "team_name": (history.get("entry") or {}).get("name"),
        "gameweek": gw,
        "summary": {"gameweek_points": row.get("points"), "total_points": row.get("total_points"),
                    "points_on_bench": row.get("points_on_bench")},
        "squad": players,
        "automatic_subs": subs,
    }


@server.tool()
async def get_my_fpl_leagues(entry_id: Optional[int] = None, game: Game = None) -> dict:
    """
    Description:
        Lists the mini-leagues the owner's manager is a member of, with the manager's current
        rank in each: classic (points-based) leagues and/or FPL Draft leagues (points or
        head-to-head). Use this to discover league ids (e.g. to then fetch a specific
        league's standings) or to answer "which leagues am I in / what's my rank".

    Parameters:
    entry_id: Optional[int] = None
        The manager ("entry") id — a classic or a Draft id depending on `game` (classic
        when `game` is omitted). If omitted, the owner's configured default team(s) are used.

    game: "classic" | "draft" | None = None
        Limit the answer to one game. If omitted, every game the owner's key is linked to
        is listed (both, for a key linked to a classic and a Draft team).

    Output:
        For one game: the manager's name and their leagues (league id, name, the manager's
        rank; Draft leagues also say whether they are points or head-to-head). For both
        games: the same under `classic` and `draft`.
    """
    if game in ("classic", "draft"):
        games = [game]
    elif entry_id is not None:
        games = ["classic"]
    else:
        games = linked_games() or ["classic"]
    if games == ["draft"]:
        return await _draft_leagues(entry_id)
    if games == ["classic"]:
        return await _classic_leagues(entry_id)
    return {"classic": await _classic_leagues(None), "draft": await _draft_leagues(None)}


async def _draft_leagues(entry_id: Optional[int]) -> dict:
    eid = resolve_draft_entry_id(entry_id)
    if eid is None:
        return _no_draft_entry()
    logger.info(f"Getting FPL Draft leagues for entry={eid}")
    try:
        entry = (await fetch_draft(f"entry/{eid}/public")).get("entry", {}) or {}
        details = await asyncio.gather(*(fetch_draft_league(lid) for lid in entry.get("league_set", [])))
    except Exception as e:
        return fpl_error("Error while getting the manager's FPL Draft leagues", e)

    leagues = []
    for d in details:
        league = DraftLeague(d)
        me = next((e for e in league.entries if e.get("entry_id") == eid), {})
        row = next((r for r in d.get("standings", []) if r.get("league_entry") == me.get("id")), {})
        leagues.append({"id": league.league.get("id"), "name": league.name,
                        "scoring": "head-to-head" if league.is_h2h else "points",
                        "my_rank": row.get("rank")})
    return {
        "game": "draft",
        "manager_name": f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}".strip(),
        "team_name": entry.get("name"),
        "leagues_count": len(leagues),
        "leagues": leagues,
    }


async def _classic_leagues(entry_id: Optional[int]) -> dict:
    eid = _fpl_entry_id(entry_id)
    if eid is None:
        return {
            "Error": "No FPL manager id is configured.",
            "Tip": missing_entry_tip(),
        }
    logger.info(f"Getting FPL leagues for entry={eid}")
    try:
        entry = await fetch_fpl(f"entry/{eid}/")
    except Exception as e:
        return fpl_error("Error while getting the manager's leagues", e)

    leagues = [
        {"id": l.get("id"), "name": l.get("name"), "my_rank": l.get("entry_rank")}
        for l in (entry.get("leagues", {}) or {}).get("classic", [])
    ]
    return {
        "manager_name": f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}".strip(),
        "team_name": entry.get("name"),
        "leagues_count": len(leagues),
        "leagues": leagues,
    }


@server.tool()
async def get_fpl_league_standings(league_id: Optional[int] = None,
                                   limit: int = 25,
                                   game: Game = None,
                                   gameweek: Optional[int] = None) -> dict:
    """
    Description:
        Returns the standings (table) of a mini-league: the ranked managers with their team
        name, total points and last-gameweek points. The owner's own row is flagged. Use this
        for "how's my league doing / what's the table in my league". For an FPL Draft
        head-to-head league the table has wins/draws/losses and league points, and the
        gameweek's head-to-head matches are included ("who do I play this week").

    Parameters:
    league_id: Optional[int] = None
        The league id to fetch — a classic or a Draft id depending on `game`. If omitted,
        the owner's configured default league is used. Use get_my_fpl_leagues to discover
        league ids.

    limit: int = 25
        Maximum number of ranked entries to return (from the top of the table).

    game: "classic" | "draft" | None = None
        Which game: classic FPL or FPL Draft (separate games with separate ids). Leave it
        out when the owner's key is linked to one game only; the tool then uses that game.

    gameweek: Optional[int] = None
        Draft head-to-head leagues only: whose matches to list. If omitted, the gameweek in
        play — the current one, or the next one once the current one has finished.

    Output:
        JSON object with the league name and the ranked standings (rank, team name,
        manager name, total points, last-gameweek points), with the owner's row marked;
        for a Draft H2H league also `matches` for the gameweek.
    """
    which = resolve_game(game)
    if isinstance(which, dict):
        return which
    if which == "draft":
        return await _draft_standings(league_id, limit, gameweek)
    lid = _fpl_league_id(league_id)
    if lid is None:
        return {
            "Error": "No FPL league id is configured.",
            "Tip": missing_league_tip(),
        }
    logger.info(f"Getting FPL standings for league={lid}")
    try:
        data = await fetch_fpl(f"leagues-classic/{lid}/standings/")
    except Exception as e:
        return fpl_error("Error while getting the league standings", e)

    my_entry = _fpl_entry_id(None)
    results = (data.get("standings", {}) or {}).get("results", [])
    table = [
        {
            "rank": r.get("rank"),
            "team_name": r.get("entry_name"),
            "manager": r.get("player_name"),
            "total_points": r.get("total"),
            "gameweek_points": r.get("event_total"),
            "is_me": my_entry is not None and r.get("entry") == my_entry,
        }
        for r in results[: max(1, limit)]
    ]
    return {
        "league_id": lid,
        "league_name": (data.get("league", {}) or {}).get("name"),
        "entries_shown": len(table),
        "standings": table,
    }


async def _draft_standings(league_id: Optional[int], limit: int, gameweek: Optional[int]) -> dict:
    lid = resolve_draft_league_id(league_id)
    if lid is None:
        return _no_draft_league()
    logger.info(f"Getting FPL Draft standings for league={lid}")
    try:
        details = await fetch_draft_league(lid)
        game_state = await fetch_draft_game()
    except Exception as e:
        return fpl_error("Error while getting the FPL Draft league", e)

    league = DraftLeague(details)
    my_entry = resolve_draft_entry_id(None)
    table = []
    for r in (details.get("standings") or [])[: max(1, limit)]:
        who = league.by_league_entry_id(r.get("league_entry"))
        row = {"rank": r.get("rank"), "team_name": who["team_name"], "manager": who["manager"],
               "is_me": my_entry is not None and who["entry_id"] == my_entry}
        if league.is_h2h:
            row.update({"league_points": r.get("total"), "won": r.get("matches_won"),
                        "drawn": r.get("matches_drawn"), "lost": r.get("matches_lost"),
                        "points_for": r.get("points_for"), "points_against": r.get("points_against")})
        else:
            row.update({"total_points": r.get("total"), "gameweek_points": r.get("event_total")})
        table.append(row)

    out = {"game": "draft", "league_id": lid, "league_name": league.name,
           "scoring": "head-to-head" if league.is_h2h else "points",
           "entries_shown": len(table), "standings": table}
    if league.is_h2h:
        gw = draft_gameweek(game_state, gameweek, prefer="upcoming")
        matches = []
        for m in details.get("matches") or []:
            if m.get("event") != gw:
                continue
            home = league.by_league_entry_id(m.get("league_entry_1"))
            away = league.by_league_entry_id(m.get("league_entry_2"))
            match = {"team_1": home["team_name"], "manager_1": home["manager"],
                     "team_2": away["team_name"], "manager_2": away["manager"],
                     "started": m.get("started"), "finished": m.get("finished"),
                     "involves_me": my_entry is not None and my_entry in (home["entry_id"], away["entry_id"])}
            if m.get("started"):
                match["score"] = f"{m.get('league_entry_1_points')}-{m.get('league_entry_2_points')}"
            matches.append(match)
        out["matches_gameweek"] = gw
        out["matches"] = matches
    return out


def _manager_matches(entry_row: dict, query: str) -> bool:
    """Case-insensitive match of a free-text manager query against a snapshot manager's
    name or team name."""
    q = (query or "").strip().lower()
    if not q:
        return True
    return q in (entry_row.get("manager") or "").lower() or q in (entry_row.get("team") or "").lower()


@server.tool()
async def get_league_history(view: Literal["standings", "squads"] = "standings",
                             days: int = 30,
                             manager: Optional[str] = None,
                             league_id: Optional[int] = None) -> dict:
    """
    Description:
        Returns the mini-league's HISTORY OVER TIME from this server's own database (a daily
        snapshot is recorded at 04:00 for every manager: their rank, points, and full squad).
        Use this for change-over-time questions the live standings tool cannot answer:
        "kto najbardziej awansował w tabeli", "jak zmieniły się moje punkty w miesiąc",
        "kogo miał w składzie X dwa tygodnie temu", "kto wpadł/wypadł ze składu".
        For the CURRENT table use get_fpl_league_standings; for CURRENT ownership use
        get_league_ownership / who_owns_player_in_league.

    Parameters:
    view: "standings" | "squads" = "standings"
        - "standings": rank + points movement. Without `manager`: every manager's rank and
          total at the start vs end of the window, with deltas (who climbed/fell, who gained
          most points). With `manager`: that manager's full daily series (rank, total, GW pts).
        - "squads": which footballers managers had, grouped by gameweek. With `manager`: that
          manager's squad per gameweek in the window. Without `manager`: the most recent
          snapshot's squads for all managers.

    days: int = 30
        How far back to look, in calendar days. Defaults to 30.

    manager: Optional[str] = None
        Free-text filter on a manager's name or team name (e.g. 'Kuba', 'Wombat FC'). Omit
        for the whole league.

    league_id: Optional[int] = None
        The classic league id. If omitted, the owner's configured default league is used.

    Output:
        JSON with the resolved league/window and the requested view; or a message when no
        history has been recorded yet (the collector needs a configured database and at least
        one daily run, or a backfill). Classic leagues only (no Draft history yet).
    """
    if _draft_only_caller(league_id):
        return {"message": "League history is recorded for classic FPL leagues only; there is no "
                           "FPL Draft history yet. For the current Draft table use "
                           "get_fpl_league_standings."}
    lid = _fpl_league_id(league_id)
    if lid is None:
        return {
            "Error": "No FPL league id is configured.",
            "Tip": missing_league_tip(),
        }
    try:
        days = max(1, int(days))
    except (TypeError, ValueError):
        days = 30
    start = date.today() - timedelta(days=days)
    logger.info(f"Reading FPL league {lid} history (view={view}, days={days}, manager={manager!r})")

    snapshots = await asyncio.to_thread(load_league_history, lid, start, None)
    if not snapshots:
        return {
            "league_id": lid,
            "message": "Brak zapisanej historii ligi w tym oknie. Historia zbierana jest "
                       "codziennie o 4:00 i wymaga skonfigurowanej bazy (DATABASE_URL); jeśli "
                       "dopiero co ją włączono, dane pojawią się po pierwszym zbiorze.",
        }

    first, last = snapshots[0], snapshots[-1]
    league_name = last["data"].get("league_name")
    window = {"from": first["date"], "to": last["date"], "snapshots": len(snapshots)}

    if view == "standings":
        if manager:
            series = []
            for snap in snapshots:
                row = next((m for m in snap["data"].get("managers", []) if _manager_matches(m, manager)), None)
                if row:
                    series.append({
                        "date": snap["date"], "gameweek": snap["gameweek"],
                        "rank": row.get("rank"), "total": row.get("total"),
                        "gameweek_points": row.get("event_total"),
                    })
            if not series:
                return {"league_id": lid, "league_name": league_name,
                        "message": f"Nie znaleziono menedżera pasującego do '{manager}' w historii."}
            return {"league_id": lid, "league_name": league_name, "manager_query": manager,
                    "window": window, "series": series}

        # League-wide: compare each manager between the first and last snapshot in the window.
        first_by_entry = {m.get("entry"): m for m in first["data"].get("managers", [])}
        movement = []
        for m in last["data"].get("managers", []):
            e = m.get("entry")
            prev = first_by_entry.get(e, {})
            movement.append({
                "manager": m.get("manager"), "team": m.get("team"),
                "rank": m.get("rank"), "rank_start": prev.get("rank"),
                "rank_delta": (prev.get("rank") - m.get("rank"))
                    if prev.get("rank") is not None and m.get("rank") is not None else None,
                "total": m.get("total"), "total_start": prev.get("total"),
                "points_gained": (m.get("total") - prev.get("total"))
                    if prev.get("total") is not None and m.get("total") is not None else None,
            })
        movement.sort(key=lambda x: x.get("rank") or 10**9)
        return {"league_id": lid, "league_name": league_name, "window": window,
                "note": "rank_delta > 0 = awans (spadek numeru miejsca); points_gained = punkty zdobyte w oknie.",
                "standings_movement": movement}

    # view == "squads"
    if manager:
        by_gw: dict = {}
        for snap in snapshots:  # later snapshots overwrite earlier for the same GW (squads are GW-stable)
            row = next((m for m in snap["data"].get("managers", []) if _manager_matches(m, manager)), None)
            if row and row.get("picks"):
                by_gw[snap["gameweek"]] = {
                    "gameweek": snap["gameweek"], "manager": row.get("manager"), "team": row.get("team"),
                    "squad": [
                        {"name": p.get("name"), "position": p.get("position"),
                         "is_captain": p.get("is_captain"), "on_bench": (p.get("pick_position") or 0) > 11}
                        for p in row["picks"]
                    ],
                }
        if not by_gw:
            return {"league_id": lid, "league_name": league_name,
                    "message": f"Brak zapisanych składów dla menedżera '{manager}' w tym oknie."}
        return {"league_id": lid, "league_name": league_name, "manager_query": manager,
                "window": window, "squads_by_gameweek": [by_gw[g] for g in sorted(by_gw)]}

    # squads, whole league: latest snapshot only (bounded output)
    latest = last["data"]
    squads = []
    for m in latest.get("managers", [])[:40]:
        if not m.get("picks"):
            continue
        squads.append({
            "manager": m.get("manager"), "team": m.get("team"), "rank": m.get("rank"),
            "squad": [{"name": p.get("name"), "is_captain": p.get("is_captain")} for p in m["picks"]],
        })
    return {"league_id": lid, "league_name": league_name, "gameweek": last["gameweek"],
            "as_of": last["date"], "managers": len(squads),
            "note": "Najświeższy zapisany zrzut składów całej ligi (dla trendu w czasie podaj konkretnego menedżera).",
            "squads": squads}


def _player_match_status(team_state: dict, team_id) -> str:
    """Human-readable state of a player's match this gameweek."""
    st = team_state.get(team_id)
    if not st:
        return "not started"
    if st.get("live"):
        minutes = st.get("minutes")
        return f"live {minutes}'" if minutes is not None else "live"
    if st.get("finished"):
        return "finished"
    return "not started"


def _player_playing_status(match_state: dict, stats: dict) -> str:
    """Whether the player is actually on the pitch, from the live feed's `starts` /
    `minutes` (which only populate once the real lineup is confirmed at kickoff).

    A player can be in the owner's FPL XI yet start the real match on the bench: that
    shows here as 'benched (not on)' while the match is live, or 'did not play (unused
    sub)' once it has finished with zero minutes."""
    starts = stats.get("starts") or 0
    minutes = stats.get("minutes") or 0
    live = bool(match_state and match_state.get("live"))
    finished = bool(match_state and match_state.get("finished"))
    if not live and not finished:
        return "match not started"
    if finished:
        if minutes and starts:
            return "played (started)"
        if minutes:
            return "played (subbed on)"
        return "did not play (unused sub)"
    # match is live
    if starts:
        return "playing (started)"
    if minutes:
        return "playing (subbed on)"
    return "benched (not on)"


@server.tool()
async def get_fpl_live(gameweek: Optional[int] = None,
                       include_my_players: bool = True,
                       entry_id: Optional[int] = None,
                       game: Game = None) -> dict:
    """
    Description:
        Real-time state of the gameweek: which Premier League matches are being played
        right now, what is happening in them (live score, minute, goals, assists, cards)
        and — for the owner — how each of their FPL players is doing live (minutes,
        goals, bonus, live points, and whether that player's match is in play). Use this
        for "are there matches on now / what's the score / how are my players doing".

        Each of the owner's players also carries `started` and `playing_status`, which
        reflect the REAL match — 'playing (started)', 'playing (subbed on)', 'benched
        (not on)', 'played/did not play' — so you can tell a player who is actually on
        the pitch from one the owner has in their FPL XI but who is sitting on the real
        bench. `my_players.starters_not_playing` lists exactly those benched/unused XI
        players. This becomes known only once lineups are confirmed at kickoff.

    Parameters:
    gameweek: Optional[int] = None
        The gameweek to inspect. If omitted, the round currently in play is used
        (the current gameweek while it is unfinished, else the next one).

    include_my_players: bool = True
        When true, also return the owner's squad with live per-player stats. Set false
        to only get the match scores/events (e.g. when no manager id is configured).

    entry_id: Optional[int] = None
        The manager ("entry") id for the squad section — a classic or a Draft id depending
        on `game`. If omitted, the owner's configured default team for that game is used.

    game: "classic" | "draft" | None = None
        Whose squad `my_players` follows: the classic FPL team or the FPL Draft team. Leave it
        out when the owner's key is linked to one game only. Match scores are the same for both.

    Output:
        JSON object with `any_live` (is any match in play), `live_matches` (in-progress
        matches with live score, minute and events), `finished_matches` (final scores
        for matches already played this gameweek) and, when requested, `my_players`
        (each squad player's live stats, match status and provisional points).
    """
    logger.info(f"Getting FPL live state for gameweek={gameweek or 'in-play'}")
    bootstrap, gw = None, None  # referenced by the error hint if the try fails early
    try:
        bootstrap = await fetch_fpl_bootstrap()
        gw = gameweek if gameweek is not None else relevant_gameweek(bootstrap)
        if gw is None:
            return {"Error": "Could not determine a gameweek (no events in the FPL calendar)."}
        teams_by_id, elements_by_id, positions_by_id = index_bootstrap(bootstrap)
        fixtures = await fetch_fpl(f"fixtures/?event={gw}")
    except Exception as e:
        return fpl_error("Error while getting the live FPL state", e,
                         bootstrap=bootstrap, gameweek=gw)

    # A fixture is "live" once it has started but before it is provisionally finished
    # (finished_provisional flips to true at full time, before bonus is finalized).
    live_matches, finished_matches = [], []
    for f in fixtures:
        if not f.get("started"):
            continue
        home = teams_by_id.get(f.get("team_h"), {})
        away = teams_by_id.get(f.get("team_a"), {})
        base = {
            "home": home.get("name"),
            "away": away.get("name"),
            "score": f"{f.get('team_h_score')}-{f.get('team_a_score')}",
        }
        if f.get("finished_provisional"):
            finished_matches.append(base)
        else:
            live = dict(base)
            live["minutes"] = f.get("minutes")
            live["events"] = summarize_fixture_events(f, elements_by_id, teams_by_id)
            live_matches.append(live)

    result: dict = {
        "gameweek": gw,
        "any_live": bool(live_matches),
        "live_matches": live_matches,
        "finished_matches": finished_matches,
    }
    if not live_matches and not finished_matches:
        result["note"] = "No matches in this gameweek have kicked off yet."

    if include_my_players and gameweek is None and not live_matches and not finished_matches:
        # Nothing has kicked off in the round in play: it has not reached its deadline (or
        # has only just), so squads for it are not public yet — say so instead of a 404.
        result["my_players"] = {
            "note": f"Gameweek {gw} has not started, so there is nothing live for the owner's "
                    "players yet (squads are published after the deadline). For the latest "
                    "squad and its points use get_my_fpl_squad."}
        return result

    if include_my_players:
        which = resolve_game(game)
        if isinstance(which, dict):
            result["my_players"] = which
            return result
        eid = resolve_draft_entry_id(entry_id) if which == "draft" else _fpl_entry_id(entry_id)
        if eid is None:
            result["my_players"] = _no_draft_entry() if which == "draft" else {
                "Error": "No FPL manager id is configured.",
                "Tip": missing_entry_tip(),
            }
            return result
        try:
            live_data = await fetch_fpl(f"event/{gw}/live/")
            if which == "draft":
                picks = await _draft_picks_as_classic(eid, gw, bootstrap)
            else:
                picks = (await fetch_fpl(f"entry/{eid}/event/{gw}/picks/")).get("picks", [])
        except Exception as e:
            result["my_players"] = fpl_error(
                "Error while getting the squad's live performance", e,
                bootstrap=bootstrap, gameweek=gw,
            )
            return result

        live_by_element = {e.get("id"): e.get("stats", {}) for e in live_data.get("elements", [])}
        # Map each team to the state of its match this gameweek (prefer a live fixture
        # if a team somehow has more than one, e.g. a double gameweek).
        team_state: dict = {}
        for f in fixtures:
            info = {
                "live": bool(f.get("started") and not f.get("finished_provisional")),
                "finished": bool(f.get("finished_provisional")),
                "minutes": f.get("minutes"),
            }
            for team_id in (f.get("team_h"), f.get("team_a")):
                prev = team_state.get(team_id)
                if prev is None or (info["live"] and not prev["live"]):
                    team_state[team_id] = info

        players = []
        provisional_points = 0
        for p in picks:
            element = elements_by_id.get(p.get("element"), {})
            stats = live_by_element.get(p.get("element"), {})
            team_id = element.get("team")
            multiplier = p.get("multiplier", 0)
            points = stats.get("total_points", 0) or 0
            counted = points * multiplier
            provisional_points += counted
            players.append({
                "name": element.get("web_name"),
                "team": teams_by_id.get(team_id, {}).get("short_name"),
                "position": positions_by_id.get(element.get("element_type"), {}).get("singular_name_short"),
                "on_bench": p.get("position", 0) > 11,  # on the owner's FPL bench
                "is_captain": p.get("is_captain", False),
                "is_vice_captain": p.get("is_vice_captain", False),
                "multiplier": multiplier,
                "match_status": _player_match_status(team_state, team_id),
                # whether the player actually featured in the real match (started / came
                # off the bench / benched) — distinct from the owner's FPL bench above.
                "started": bool(stats.get("starts")),
                "playing_status": _player_playing_status(team_state.get(team_id), stats),
                "minutes": stats.get("minutes"),
                "goals": stats.get("goals_scored"),
                "assists": stats.get("assists"),
                "yellow_cards": stats.get("yellow_cards"),
                "red_cards": stats.get("red_cards"),
                "bonus": stats.get("bonus"),
                "points": points,               # raw FPL points for the player
                "points_counted": counted,      # after captain/bench multiplier
            })

        # Players the owner fielded (in their FPL XI, not their own bench) who are NOT on
        # the pitch in the real match — benched now, or an unused sub once it is over.
        # These are the ones silently costing points, so surface them explicitly.
        starters_not_playing = [
            p["name"] for p in players
            if not p["on_bench"] and p["playing_status"] in ("benched (not on)", "did not play (unused sub)")
        ]

        result["my_players"] = {
            "game": which,
            "entry_id": eid,
            "provisional_gameweek_points": provisional_points,
            "starters_not_playing": starters_not_playing,
            "note": "Provisional live points (starting XI x multiplier). 'playing_status' "
                    "reflects the REAL match (started/subbed on/benched), which FPL only "
                    "exposes once lineups are confirmed at kickoff; before that a benched "
                    "player is indistinguishable from one whose match has not started. FPL "
                    "applies bench auto-substitutions only after a player's match finishes.",
            "players": players,
        }

    return result


async def _draft_picks_as_classic(entry_id: int, gw: int, classic_bootstrap: dict) -> list[dict]:
    """A Draft squad as classic-shaped picks (classic element ids, matched on the player's
    code), so the live view can reuse the classic live feed. Draft has no captain; the
    bench (positions 12-15) counts zero until FPL Draft makes an automatic substitution."""
    draft_bootstrap = await fetch_draft_bootstrap()
    picks = (await fetch_draft(f"entry/{entry_id}/event/{gw}")).get("picks", [])
    to_classic = classic_ids_by_draft_id(draft_bootstrap, classic_bootstrap)
    return [{"element": to_classic.get(p.get("element")), "position": p.get("position", 0),
             "multiplier": 1 if p.get("position", 0) <= 11 else 0,
             "is_captain": False, "is_vice_captain": False}
            for p in picks]


@server.tool()
async def who_owns_player_in_league(player: str,
                                    league_id: Optional[int] = None,
                                    gameweek: Optional[int] = None,
                                    max_managers: int = _LEAGUE_SCAN_DEFAULT,
                                    game: Game = None) -> dict:
    """
    Description:
        Finds which managers in a Fantasy Premier League mini-league own a given player,
        and how many. Answers questions like "does anyone else in my league have Dalot"
        or "how many people have Haaland". It scans each manager's squad for the
        gameweek, so it also knows who has captained the player or has him on their bench.
        In an FPL Draft league a player has at most one owner: the answer is that manager,
        or that the player is free to pick up (waivers / free agency).

    Parameters:
    player: str
        The player's name (e.g. 'Haaland', 'Dalot', 'Bruno Fernandes'). Resolved against
        the FPL player list; if the name is ambiguous the tool returns the candidates so
        you can ask the user which one they meant.

    league_id: Optional[int] = None
        The classic league to scan. If omitted, the owner's configured
        default league is used. Use get_my_fpl_leagues to discover league ids.

    gameweek: Optional[int] = None
        The gameweek whose squads to check. If omitted, the current gameweek is used.

    max_managers: int = 50
        Safety cap on how many managers (from the top of the table by rank) to scan.

    game: "classic" | "draft" | None = None
        Which game: classic FPL or FPL Draft (separate games with separate ids). Leave it
        out when the owner's key is linked to one game only; the tool then uses that game.

    Output:
        JSON object with the resolved player, the league, how many of the scanned
        managers own the player (with in-league ownership %) and the list of owners
        (manager, team name, rank, whether captained or benched by them). Draft: the
        owner (or none) and whether the player is available.
    """
    which = resolve_game(game)
    if isinstance(which, dict):
        return which
    if which == "draft":
        return await _draft_owner(player, league_id)
    lid = _fpl_league_id(league_id)
    if lid is None:
        return {
            "Error": "No FPL league id is configured.",
            "Tip": missing_league_tip(),
        }
    logger.info(f"Scanning league {lid} for owners of '{player}'")
    bootstrap, gw = None, None  # referenced by the error hint if the try fails early
    try:
        bootstrap = await fetch_fpl_bootstrap()
        gw = resolve_gameweek(bootstrap, gameweek, prefer="current")
        if gw is None:
            return {"Error": "Could not determine a gameweek (no events in the FPL calendar)."}
        teams_by_id, elements_by_id, _ = index_bootstrap(bootstrap)
    except Exception as e:
        return fpl_error("Error while scanning the league", e,
                         bootstrap=bootstrap, gameweek=gw)

    matches = find_players_by_name(bootstrap, player)
    if not matches:
        return {"Error": f"No FPL player matched '{player}'."}
    if len(matches) > 1:
        return {
            "ambiguous_player": player,
            "candidates": [
                {"name": m.get("web_name"),
                 "full_name": f"{m.get('first_name','')} {m.get('second_name','')}".strip(),
                 "team": teams_by_id.get(m.get("team"), {}).get("short_name")}
                for m in matches[:10]
            ],
            "Tip": "Ask the user which player they meant, then call again with a more specific name.",
        }

    target = matches[0]
    target_id = target["id"]
    try:
        league_name, pairs = await load_league_squads(lid, gw, max(1, max_managers))
    except Exception as e:
        return fpl_error("Error while fetching the league squads", e,
                         bootstrap=bootstrap, gameweek=gw)

    owners, scanned, failed = [], 0, 0
    for row, picks in pairs:
        if picks is None:
            failed += 1
            continue
        scanned += 1
        pick = next((p for p in picks if p.get("element") == target_id), None)
        if pick:
            owners.append({
                "manager": row.get("player_name"),
                "team_name": row.get("entry_name"),
                "rank": row.get("rank"),
                "is_captain": pick.get("is_captain", False),
                "on_their_bench": pick.get("position", 0) > 11,
            })

    owners.sort(key=lambda o: o.get("rank") or 10**9)
    return {
        "player": target.get("web_name"),
        "team": teams_by_id.get(target.get("team"), {}).get("short_name"),
        "league_id": lid,
        "league_name": league_name,
        "gameweek": gw,
        "managers_scanned": scanned,
        "owner_count": len(owners),
        "ownership_pct_in_league": round(100 * len(owners) / scanned, 1) if scanned else None,
        "owners": owners,
        "unavailable_squads": failed or None,
    }


def _ambiguous_players(query: str, matches: list[dict], teams_by_id: dict) -> dict:
    return {
        "ambiguous_player": query,
        "candidates": [
            {"name": m.get("web_name"),
             "full_name": f"{m.get('first_name', '')} {m.get('second_name', '')}".strip(),
             "team": teams_by_id.get(m.get("team"), {}).get("short_name")}
            for m in matches[:10]
        ],
        "Tip": "Ask the user which player they meant, then call again with a more specific name.",
    }


async def _draft_owner(player: str, league_id: Optional[int]) -> dict:
    lid = resolve_draft_league_id(league_id)
    if lid is None:
        return _no_draft_league()
    logger.info(f"Looking up the owner of '{player}' in FPL Draft league {lid}")
    try:
        bootstrap = await fetch_draft_bootstrap()
        statuses, details = await asyncio.gather(fetch_draft_element_status(lid), fetch_draft_league(lid))
    except Exception as e:
        return fpl_error("Error while checking the FPL Draft league", e)

    teams_by_id, _, positions_by_id = index_draft_bootstrap(bootstrap)
    matches = find_players_by_name(bootstrap, player)
    if not matches:
        return {"Error": f"No FPL player matched '{player}'."}
    if len(matches) > 1:
        return _ambiguous_players(player, matches, teams_by_id)

    target = matches[0]
    league = DraftLeague(details)
    status = next((x for x in statuses if x.get("element") == target["id"]), {})
    owner = status.get("owner")
    my_entry = resolve_draft_entry_id(None)
    out = {"game": "draft", **describe_draft_player(target, teams_by_id, positions_by_id),
           "league_id": lid, "league_name": league.name}
    if owner is not None:
        out["owned"] = True
        out["owner"] = {**league.by_entry(owner), "is_me": owner == my_entry}
        out["in_pending_trade"] = bool(status.get("in_accepted_trade"))
    else:
        out["owned"] = False
        out["available_via"] = ("waivers" if league.transaction_mode == "waivers" else "free agency")
        out["note"] = ("Unowned in this league. Under waivers a claim is processed at the waiver "
                       "deadline by waiver priority; outside the waiver window it can be picked up "
                       "as a free agent.")
    return out


@server.tool()
async def get_league_ownership(league_id: Optional[int] = None,
                               gameweek: Optional[int] = None,
                               top_n: int = 20,
                               max_managers: int = _LEAGUE_SCAN_DEFAULT) -> dict:
    """
    Description:
        Aggregates squad ownership across a Fantasy Premier League mini-league: which
        players are the most-owned within the league (effective in-league ownership) and
        how often each is captained. Use it for "what's popular in my league / most
        picked players / template" questions. To check one specific player instead, use
        who_owns_player_in_league.

    Parameters:
    league_id: Optional[int] = None
        The classic league to scan. If omitted, the owner's configured
        default league is used.

    gameweek: Optional[int] = None
        The gameweek whose squads to aggregate. If omitted, the current gameweek is used.

    top_n: int = 20
        How many of the most-owned players to return.

    max_managers: int = 50
        Safety cap on how many managers (from the top of the table by rank) to scan.

    Output:
        JSON object with the league, how many managers were scanned and the most-owned
        players (name, team, position, how many managers own them, in-league ownership %
        and how many captained them). Classic leagues only: in FPL Draft every player has at
        most one owner — use who_owns_player_in_league or get_draft_free_agents instead.
    """
    if _draft_only_caller(league_id):
        return {"message": "Ownership rankings only make sense in classic FPL: in a Draft league each "
                           "player belongs to at most one manager. Use who_owns_player_in_league "
                           "(who has a player) or get_draft_free_agents (who is still free)."}
    lid = _fpl_league_id(league_id)
    if lid is None:
        return {
            "Error": "No FPL league id is configured.",
            "Tip": missing_league_tip(),
        }
    logger.info(f"Aggregating ownership for league {lid}")
    bootstrap, gw = None, None  # referenced by the error hint if the try fails early
    try:
        bootstrap = await fetch_fpl_bootstrap()
        gw = resolve_gameweek(bootstrap, gameweek, prefer="current")
        if gw is None:
            return {"Error": "Could not determine a gameweek (no events in the FPL calendar)."}
        teams_by_id, elements_by_id, positions_by_id = index_bootstrap(bootstrap)
        league_name, pairs = await load_league_squads(lid, gw, max(1, max_managers))
    except Exception as e:
        return fpl_error("Error while aggregating league ownership", e,
                         bootstrap=bootstrap, gameweek=gw)

    owned: dict = {}
    captained: dict = {}
    scanned = 0
    for _, picks in pairs:
        if picks is None:
            continue
        scanned += 1
        for p in picks:
            el = p.get("element")
            owned[el] = owned.get(el, 0) + 1
            if p.get("is_captain"):
                captained[el] = captained.get(el, 0) + 1

    ranked = sorted(owned.items(), key=lambda kv: kv[1], reverse=True)[: max(1, top_n)]
    top_owned = []
    for el, count in ranked:
        element = elements_by_id.get(el, {})
        top_owned.append({
            "name": element.get("web_name"),
            "team": teams_by_id.get(element.get("team"), {}).get("short_name"),
            "position": positions_by_id.get(element.get("element_type"), {}).get("singular_name_short"),
            "owned_by": count,
            "ownership_pct_in_league": round(100 * count / scanned, 1) if scanned else None,
            "captained_by": captained.get(el, 0),
        })

    return {
        "league_id": lid,
        "league_name": league_name,
        "gameweek": gw,
        "managers_scanned": scanned,
        "top_owned": top_owned,
    }


_POSITIONS = {"GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}
_SORT_KEYS = {"form": "form", "total_points": "total_points", "points_per_game": "points_per_game",
              "expected_points": "ep_next"}


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@server.tool()
async def get_draft_free_agents(position: Optional[Literal["GKP", "DEF", "MID", "FWD"]] = None,
                                sort_by: Literal["form", "total_points", "points_per_game",
                                                 "expected_points"] = "form",
                                limit: int = 15,
                                include_unavailable: bool = False,
                                league_id: Optional[int] = None) -> dict:
    """
    Description:
        FPL Draft only. The best players nobody owns in the owner's Draft league — the
        waiver / free-agent targets — ranked by form, total points, points per game or
        expected points next gameweek, with each player's next opponent and its FDR. Use it
        for "who should I pick up / who is on the waiver wire / best free defender".

    Parameters:
    position: "GKP" | "DEF" | "MID" | "FWD" | None = None
        Only players in this position. If omitted, all positions.

    sort_by: "form" | "total_points" | "points_per_game" | "expected_points" = "form"
        How to rank the free players (expected_points = FPL's expected points next gameweek).

    limit: int = 15
        How many players to return.

    include_unavailable: bool = False
        Also list injured, suspended or otherwise unavailable players (doubtful ones are
        always listed, with their news).

    league_id: Optional[int] = None
        The Draft league. If omitted, the owner's configured default Draft league is used.

    Output:
        JSON with the league, how players can be added there (waivers or free agency) and the
        ranked free players (name, team, position, form, points, points per game, expected
        points, availability news and next fixture with difficulty 1-5).
    """
    lid = resolve_draft_league_id(league_id)
    if lid is None:
        return _no_draft_league()
    logger.info(f"Listing FPL Draft free agents in league {lid} (position={position}, sort={sort_by})")
    try:
        bootstrap = await fetch_draft_bootstrap()
        statuses, details, game_state = await asyncio.gather(
            fetch_draft_element_status(lid), fetch_draft_league(lid), fetch_draft_game())
    except Exception as e:
        return fpl_error("Error while listing FPL Draft free agents", e)

    teams_by_id, elements_by_id, positions_by_id = index_draft_bootstrap(bootstrap)
    owned = {x.get("element") for x in statuses if x.get("owner") is not None}
    # The draft API has no expected points (ep_next); take them from classic FPL, by player code.
    expected: dict = {}
    try:
        classic = await fetch_fpl_bootstrap()
        classic_ep = {e.get("code"): e.get("ep_next") for e in classic.get("elements", [])}
        expected = {el["id"]: classic_ep.get(el.get("code")) for el in elements_by_id.values()}
    except Exception:
        logger.warning("Could not load classic expected points for free agents", exc_info=True)
    key = _SORT_KEYS.get(sort_by, "form")
    value = (lambda e: expected.get(e["id"])) if key == "ep_next" else (lambda e: e.get(key))  # noqa: E731
    pool = []
    for el in elements_by_id.values():
        if el["id"] in owned:
            continue
        if position and el.get("element_type") != _POSITIONS[position]:
            continue
        if not include_unavailable and el.get("status") in ("i", "s", "u", "n"):
            continue
        pool.append(el)
    pool.sort(key=lambda e: (_num(value(e)), _num(e.get("total_points"))), reverse=True)

    next_gw = draft_gameweek(game_state, None, prefer="upcoming")
    opponents = {}
    try:  # difficulty comes from the classic fixtures (team ids are the same in both games)
        for f in await fetch_fpl(f"fixtures/?event={next_gw}") if next_gw else []:
            for team, opp, diff, venue in ((f.get("team_h"), f.get("team_a"), f.get("team_h_difficulty"), "H"),
                                           (f.get("team_a"), f.get("team_h"), f.get("team_a_difficulty"), "A")):
                opponents.setdefault(team, []).append(
                    {"opponent": teams_by_id.get(opp, {}).get("short_name"), "home_or_away": venue,
                     "difficulty": diff})
    except Exception:
        logger.warning("Could not fetch next fixtures for free agents", exc_info=True)

    players = []
    for el in pool[: max(1, limit)]:
        info = describe_draft_player(el, teams_by_id, positions_by_id)
        info.update({"points_per_game": el.get("points_per_game"), "expected_points": expected.get(el["id"]),
                     "next_fixtures": opponents.get(el.get("team"), [])})
        players.append(info)
    league = DraftLeague(details)
    return {
        "game": "draft", "league_id": lid, "league_name": league.name,
        "adding_players": league.transaction_mode,
        "next_gameweek": next_gw, "sorted_by": sort_by, "position": position,
        "players": players,
    }


@server.tool()
async def get_draft_activity(kind: Literal["transactions", "trades", "draft"] = "transactions",
                             gameweek: Optional[int] = None,
                             manager: Optional[str] = None,
                             include_denied: bool = False,
                             limit: int = 30,
                             league_id: Optional[int] = None) -> dict:
    """
    Description:
        FPL Draft only. What has happened in the owner's Draft league: waiver claims and
        free-agent pickups ("transactions"), trades between managers ("trades"), or the
        original draft picks in order ("draft"). Use it for "who did X pick up", "what moves
        happened this week", "who traded with whom", "who did I draft in round 1".

    Parameters:
    kind: "transactions" | "trades" | "draft" = "transactions"
        Which activity to list.

    gameweek: Optional[int] = None
        Only activity for this gameweek (transactions and trades). If omitted, all of it.

    manager: Optional[str] = None
        Only activity involving this manager — free text matched against the manager's
        name or team name (e.g. 'Kuba', 'Wombat FC').

    include_denied: bool = False
        Transactions only: also list waiver claims that failed (outbid by a higher waiver
        priority, or no longer valid). Useful for "who else wanted that player".

    limit: int = 30
        How many items to return (newest first; draft picks in pick order).

    league_id: Optional[int] = None
        The Draft league. If omitted, the owner's configured default Draft league is used.

    Output:
        JSON with the league and a list of items with player names resolved: transactions
        (gameweek, manager, waiver/free agent, player in/out, result), trades (gameweek,
        offering and receiving manager, players each side gets, state) or draft picks
        (overall pick number, round, pick within the round, manager, player).
    """
    lid = resolve_draft_league_id(league_id)
    if lid is None:
        return _no_draft_league()
    logger.info(f"Getting FPL Draft {kind} for league {lid}")
    path = {"transactions": f"draft/league/{lid}/transactions", "trades": f"draft/league/{lid}/trades",
            "draft": f"draft/{lid}/choices"}[kind]
    try:
        bootstrap = await fetch_draft_bootstrap()
        data, details = await asyncio.gather(fetch_draft(path), fetch_draft_league(lid))
    except Exception as e:
        return fpl_error(f"Error while getting the FPL Draft {kind}", e)

    teams_by_id, elements_by_id, _ = index_draft_bootstrap(bootstrap)
    league = DraftLeague(details)

    def player(el) -> Optional[str]:
        e = elements_by_id.get(el)
        if not e:
            return None
        return f"{e.get('web_name')} ({teams_by_id.get(e.get('team'), {}).get('short_name')})"

    wanted = None
    if manager:
        wanted = {e.get("entry_id") for e in league.find_manager(manager)}
        if not wanted:
            return {"game": "draft", "league_id": lid, "league_name": league.name,
                    "message": f"No manager in this league matches '{manager}'."}

    items = []
    if kind == "transactions":
        for t in sorted(data.get("transactions", []), key=lambda t: t.get("added") or "", reverse=True):
            if gameweek is not None and t.get("event") != gameweek:
                continue
            if wanted is not None and t.get("entry") not in wanted:
                continue
            if not include_denied and t.get("result") != "a":
                continue
            who = league.by_entry(t.get("entry"))
            items.append({"gameweek": t.get("event"), "manager": who["manager"], "team_name": who["team_name"],
                          "type": TRANSACTION_KINDS.get(t.get("kind"), t.get("kind")),
                          "player_in": player(t.get("element_in")), "player_out": player(t.get("element_out")),
                          "result": TRANSACTION_RESULTS.get(t.get("result"), t.get("result")),
                          "time": t.get("added")})
    elif kind == "trades":
        for t in sorted(data.get("trades", []), key=lambda t: t.get("offer_time") or "", reverse=True):
            if gameweek is not None and t.get("event") != gameweek:
                continue
            if wanted is not None and not wanted & {t.get("offered_entry"), t.get("received_entry")}:
                continue
            offered, received = league.by_entry(t.get("offered_entry")), league.by_entry(t.get("received_entry"))
            items_ = t.get("tradeitem_set", []) or []
            items.append({"gameweek": t.get("event"),
                          "offered_by": offered["manager"], "offered_by_team": offered["team_name"],
                          "offered_to": received["manager"], "offered_to_team": received["team_name"],
                          # element_in goes to the offering manager, element_out leaves them
                          "offered_by_gets": [player(i.get("element_in")) for i in items_],
                          "offered_to_gets": [player(i.get("element_out")) for i in items_],
                          "state": TRADE_STATES.get(t.get("state"), t.get("state")),
                          "time": t.get("offer_time")})
    else:
        # `index` is the overall pick number; `pick` is the pick within its round (snake order)
        for c in sorted(data.get("choices", []), key=lambda c: c.get("index") or 0):
            if wanted is not None and c.get("entry") not in wanted:
                continue
            who = league.by_entry(c.get("entry"))
            items.append({"pick": c.get("index"), "round": c.get("round"),
                          "pick_in_round": c.get("pick"), "manager": who["manager"],
                          "team_name": who["team_name"] or c.get("entry_name"),
                          "player": player(c.get("element")), "auto_pick": c.get("was_auto")})

    out = {"game": "draft", "league_id": lid, "league_name": league.name, "kind": kind,
           "count": len(items[: max(1, limit)]), "total_matching": len(items),
           "items": items[: max(1, limit)]}
    if kind == "draft" and not data.get("choices"):
        out["message"] = ("FPL publishes no draft picks for this league (the list is empty in the "
                          "FPL Draft API), so the draft order is not available.")
    return out

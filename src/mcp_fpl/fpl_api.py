"""FPL API helpers: keyless fetch, bootstrap reference-data caching (memory → Postgres →
API), id→name indexing, league squad scans and daily league history."""

import asyncio
import os
import time
import requests
from typing import Literal, Optional, Union
import logging

logger = logging.getLogger(__name__)



# ------- Fantasy Premier League (unofficial API) helpers -------
#
# The public FPL API (https://fantasy.premierleague.com/api/) is keyless and returns
# everything referenced by numeric ids (teams, players/"elements", positions,
# gameweeks/"events"). These helpers fetch the reference data once and expose plain
# lookup dicts so the tools can translate ids -> human-readable names on the server
# side, in the same spirit as the devices/maps tools: the model works with names, not
# raw numeric handles.

FPL_BASE = "https://fantasy.premierleague.com/api"
# The API blocks requests without a browser-like User-Agent, so send one.
FPL_HEADERS = {"User-Agent": "Mozilla/5.0 (Jarvis personal assistant)"}

# bootstrap-static is large (~700 players) and only changes ~daily (prices/form/news).
# It is cached on two levels: an in-process L1 memo with a short TTL (avoids a DB round
# trip within a single turn) and, when a database is configured, a persistent Postgres
# cache shared across processes/restarts (the "our database" store the owner asked for).
# On a miss at both levels we hit the API and write through to both caches.
_FPL_BOOTSTRAP_CACHE: dict = {"data": None, "fetched_at": 0.0}
_FPL_L1_TTL = 300  # seconds; in-process memo


def _fpl_db_ttl() -> int:
    """Max age (seconds) the Postgres bootstrap cache may reach before it is refreshed
    from the API. Configurable via FPL_CACHE_TTL_SECONDS; defaults to 6 hours."""
    raw = os.getenv("FPL_CACHE_TTL_SECONDS")
    try:
        return int(raw) if raw and raw.strip() else 21600
    except ValueError:
        return 21600


async def fetch_fpl(path: str) -> Union[dict, list]:
    """GET a JSON document from the FPL API. `path` is relative to FPL_BASE
    (e.g. 'fixtures/?event=2'). Runs the blocking request off the event loop so
    concurrent FPL tool calls don't serialize. Raises on HTTP errors."""
    url = f"{FPL_BASE}/{path.lstrip('/')}"

    def _do():
        resp = requests.get(url, headers=FPL_HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json()

    return await asyncio.to_thread(_do)


def _load_bootstrap_from_db(max_age: int) -> Optional[dict]:
    """Read a fresh-enough bootstrap snapshot from Postgres, or None. Best-effort:
    any error (no DB configured, connection/schema issue) falls back to the API."""
    try:
        from mcp_fpl.db import connection as dbconn
        if not dbconn.is_configured():
            return None
        from mcp_fpl.db.reference_repo import get_cached_bootstrap
        return get_cached_bootstrap(max_age)
    except Exception as e:  # pragma: no cover - defensive; DB is optional
        logger.warning("FPL bootstrap DB cache read failed (%s); using API", e)
        return None


def _save_bootstrap_to_db(data: dict) -> None:
    """Write the fresh bootstrap snapshot to Postgres. Best-effort (optional DB)."""
    try:
        from mcp_fpl.db import connection as dbconn
        if not dbconn.is_configured():
            return
        from mcp_fpl.db.reference_repo import save_bootstrap
        save_bootstrap(data)
    except Exception as e:  # pragma: no cover - defensive; DB is optional
        logger.warning("FPL bootstrap DB cache write failed (%s)", e)


async def fetch_fpl_bootstrap(force: bool = False) -> dict:
    """Fetch (and cache) the bootstrap-static reference document: teams, players
    ('elements'), positions ('element_types') and gameweeks ('events').

    Resolution order: in-process L1 memo -> Postgres cache (if configured & fresh) ->
    live API (written through to both caches). `force=True` bypasses all caches."""
    now = time.time()
    cache = _FPL_BOOTSTRAP_CACHE
    if not force and cache["data"] is not None and now - cache["fetched_at"] < _FPL_L1_TTL:
        return cache["data"]

    if not force:
        cached = await asyncio.to_thread(_load_bootstrap_from_db, _fpl_db_ttl())
        if cached is not None:
            cache["data"], cache["fetched_at"] = cached, now
            return cached

    data = await fetch_fpl("bootstrap-static/")
    cache["data"], cache["fetched_at"] = data, now
    await asyncio.to_thread(_save_bootstrap_to_db, data)
    return data


# ------- Mini-league squad scan + Postgres cache -------
#
# Aggregating who-owns-what across a mini-league means fetching every manager's picks for
# a gameweek — dozens of API calls — and it is the fpl_agent's biggest latency cost, asked
# about repeatedly. Picks for a gameweek are frozen once its deadline passes, so a whole-
# league scan is snapshotted per (league, gameweek) on two levels, exactly like the
# bootstrap: an in-process L1 memo with a short TTL and, when a database is configured, a
# persistent Postgres cache shared across processes/restarts. On a miss at both levels we
# scan the API and write through to both caches.

# Cap on how many managers a league scan will fetch squads for, and how many of those picks
# requests run at once (politeness to the unofficial API). A personal mini-league fits well
# within the cap; huge public leagues are scanned from the top by rank.
_LEAGUE_SCAN_DEFAULT = 50
_LEAGUE_SCAN_CONCURRENCY = 8

_FPL_LEAGUE_CACHE: dict = {}  # (league_id, gw) -> {pairs, league_name, complete, max, fetched_at}
_FPL_LEAGUE_L1_TTL = 300  # seconds; in-process memo


def _fpl_league_db_ttl() -> int:
    """Max age (seconds) the Postgres league-squad cache may reach before it is refreshed
    from the API. Configurable via FPL_LEAGUE_CACHE_TTL_SECONDS; defaults to 3 hours."""
    raw = os.getenv("FPL_LEAGUE_CACHE_TTL_SECONDS")
    try:
        return int(raw) if raw and raw.strip() else 10800
    except ValueError:
        return 10800


async def list_league_managers(league_id: int, max_managers: int):
    """Page a classic league's standings and return ``(league_name, ordered_rows, complete)``:
    the manager rows (deduped by entry id, ordered by rank, capped at ``max_managers``) and
    whether the whole league was captured. No squad fetching — just the table."""
    managers: dict = {}  # entry id -> row (dedupe: standings pages can overlap on ties)
    league_name = None
    page = 1
    exhausted = False
    while len(managers) < max_managers:
        data = await fetch_fpl(f"leagues-classic/{league_id}/standings/?page_standings={page}")
        league_name = league_name or (data.get("league", {}) or {}).get("name")
        standings = data.get("standings", {}) or {}
        results = standings.get("results", []) or []
        if not results:
            exhausted = True
            break
        for row in results:
            managers.setdefault(row.get("entry"), row)
        if not standings.get("has_next"):
            exhausted = True
            break
        page += 1

    # complete = we saw the entire league (ran out of pages) and it fit within the cap.
    complete = exhausted and len(managers) <= max_managers
    ordered = sorted(managers.values(), key=lambda r: r.get("rank") or 10**9)[:max_managers]
    return league_name, ordered, complete


async def _scan_league_squads(league_id: int, gw: int, max_managers: int):
    """Fetch a league's managers (deduped by entry id, ordered by rank) and each one's squad
    for gameweek ``gw`` straight from the API. Returns ``(league_name, pairs, complete)``:
    ``pairs`` is a list of ``(manager_row, picks|None)`` (None when a squad could not be
    fetched), and ``complete`` is True when the whole league was captured rather than being
    cut off at ``max_managers``. Bounded concurrency."""
    league_name, ordered, complete = await list_league_managers(league_id, max_managers)

    sem = asyncio.Semaphore(_LEAGUE_SCAN_CONCURRENCY)

    async def _picks_for(row):
        async with sem:
            try:
                pd = await fetch_fpl(f"entry/{row.get('entry')}/event/{gw}/picks/")
                return row, pd.get("picks", [])
            except Exception:
                return row, None

    pairs = list(await asyncio.gather(*(_picks_for(r) for r in ordered)))
    return league_name, pairs, complete


def _load_league_squads_from_db(league_id: int, gw: int, max_age: int, min_managers: int):
    """Read a fresh-enough league-squad snapshot from Postgres, or None. Best-effort: any
    error (no DB configured, connection/schema issue) falls back to the API."""
    try:
        from mcp_fpl.db import connection as dbconn
        if not dbconn.is_configured():
            return None
        from mcp_fpl.db.league_repo import get_cached_league_squads
        return get_cached_league_squads(league_id, gw, max_age, min_managers)
    except Exception as e:  # pragma: no cover - defensive; DB is optional
        logger.warning("FPL league squads DB cache read failed (%s); using API", e)
        return None


def _save_league_squads_to_db(league_id, gw, league_name, max_managers, complete, pairs):
    """Write a fresh league-squad snapshot to Postgres. Best-effort (optional DB)."""
    try:
        from mcp_fpl.db import connection as dbconn
        if not dbconn.is_configured():
            return
        from mcp_fpl.db.league_repo import save_league_squads
        # Tuples aren't JSON; store each (row, picks) pair as a two-item list.
        serializable = [[row, picks] for row, picks in pairs]
        save_league_squads(league_id, gw, league_name, max_managers, complete, serializable)
    except Exception as e:  # pragma: no cover - defensive; DB is optional
        logger.warning("FPL league squads DB cache write failed (%s)", e)


async def load_league_squads(
    league_id: int, gw: int, max_managers: int = _LEAGUE_SCAN_DEFAULT, force: bool = False
) -> tuple[Optional[str], list]:
    """Return ``(league_name, pairs)`` for a league+gameweek squad scan, served from cache
    when possible. ``pairs`` is a list of ``(manager_row, picks|None)``.

    Resolution order: in-process L1 memo -> Postgres cache (if configured, fresh and covering
    at least ``max_managers`` managers) -> live API scan (written through to both caches).
    ``force=True`` bypasses both caches and rescans (used by the background refresher)."""
    now = time.time()
    key = (int(league_id), int(gw))
    memo = _FPL_LEAGUE_CACHE.get(key)
    if (not force and memo is not None
            and now - memo["fetched_at"] < _FPL_LEAGUE_L1_TTL
            and (memo["complete"] or memo["max"] >= max_managers)):
        return memo["league_name"], memo["pairs"]

    if not force:
        cached = await asyncio.to_thread(
            _load_league_squads_from_db, key[0], key[1], _fpl_league_db_ttl(), max_managers
        )
        if cached is not None:
            _FPL_LEAGUE_CACHE[key] = {
                "pairs": cached["pairs"], "league_name": cached["league_name"],
                "complete": cached["complete"], "max": cached["max_managers"], "fetched_at": now,
            }
            return cached["league_name"], cached["pairs"]

    league_name, pairs, complete = await _scan_league_squads(key[0], key[1], max_managers)
    _FPL_LEAGUE_CACHE[key] = {
        "pairs": pairs, "league_name": league_name,
        "complete": complete, "max": max_managers, "fetched_at": now,
    }
    await asyncio.to_thread(
        _save_league_squads_to_db, key[0], key[1], league_name, max_managers, complete, pairs
    )
    return league_name, pairs


# ------- Permanent daily league history (positions + points + squads over time) -------
#
# Distinct from the squad *cache* above (fpl_league_squads, TTL, overwritten): this is
# append-only history written once a day by the collector (lib/runtime/observations_runner),
# so the fpl_agent can answer "how did my league's table / points / squads change over
# time". Postgres-only (no live-API fallback — it IS our accumulated record); best-effort so
# a missing DB never crashes a turn, it just yields no history.

# Cap on managers captured in a daily league-history snapshot. A friendly league fits easily;
# kept generous but bounded (politeness to the unofficial API).
_LEAGUE_HISTORY_MAX_MANAGERS = 100


def _resolve_squad(picks, elements_by_id: dict, positions_by_id: dict):
    """Turn a raw picks list into name-resolved squad entries; None passes through as None."""
    if picks is None:
        return None
    squad = []
    for p in picks:
        element = elements_by_id.get(p.get("element"), {})
        squad.append({
            "element": p.get("element"),
            "name": element.get("web_name"),
            "position": positions_by_id.get(element.get("element_type"), {}).get("singular_name_short"),
            "pick_position": p.get("position"),
            "multiplier": p.get("multiplier"),
            "is_captain": p.get("is_captain", False),
            "is_vice_captain": p.get("is_vice_captain", False),
        })
    return squad


async def build_league_history_snapshot(league_id: int, gw: int, bootstrap: dict) -> dict:
    """Scan a league's managers + squads for gameweek ``gw`` and build a JSON-serializable
    daily snapshot: every manager's standing (rank + points) AND their squad (which players
    they had), with element ids resolved to names. Forces a fresh scan (bypasses caches)."""
    _teams, elements_by_id, positions_by_id = index_bootstrap(bootstrap)
    league_name, pairs, _complete = await _scan_league_squads(
        league_id, gw, _LEAGUE_HISTORY_MAX_MANAGERS
    )
    managers = []
    for row, picks in pairs:
        managers.append({
            "entry": row.get("entry"),
            "manager": row.get("player_name"),
            "team": row.get("entry_name"),
            "rank": row.get("rank"),
            "last_rank": row.get("last_rank"),
            "total": row.get("total"),          # cumulative points
            "event_total": row.get("event_total"),  # this gameweek's points
            "picks": _resolve_squad(picks, elements_by_id, positions_by_id),
        })
    return {"gameweek": gw, "league_name": league_name, "managers": managers}


async def backfill_league_history(league_id: int, from_gw: int = 1) -> dict:
    """Reconstruct and persist one league-history snapshot per PAST gameweek, from ``from_gw``
    up to the current gameweek. For each manager we pull their season history (per-GW event/
    cumulative points from ``entry/{id}/history/``) once and their squad for each gameweek
    (``entry/{id}/event/{g}/picks/``); the in-league rank per gameweek is reconstructed by
    ordering managers on cumulative points at that gameweek. Each snapshot is dated at that
    gameweek's deadline. Postgres-only; returns a per-gameweek summary. Best-effort per call."""
    from datetime import date, datetime as _dt
    from mcp_fpl.db import connection as dbconn
    if not dbconn.is_configured():
        return {"error": "no DATABASE_URL configured"}
    from mcp_fpl.db.history_repo import save_snapshot

    bootstrap = await fetch_fpl_bootstrap()
    _teams, elements_by_id, positions_by_id = index_bootstrap(bootstrap)
    events = {e["id"]: e for e in bootstrap.get("events", [])}
    current_gw = resolve_gameweek(bootstrap, None, prefer="current")
    if current_gw is None:
        return {"error": "no current gameweek"}

    league_name, managers, _complete = await list_league_managers(league_id, _LEAGUE_HISTORY_MAX_MANAGERS)
    gws = list(range(max(1, from_gw), current_gw + 1))
    sem = asyncio.Semaphore(_LEAGUE_SCAN_CONCURRENCY)

    async def _history(row):
        async with sem:
            try:
                h = await fetch_fpl(f"entry/{row.get('entry')}/history/")
                return row, {e.get("event"): e for e in (h.get("current") or [])}
            except Exception:
                return row, {}

    hist_pairs = await asyncio.gather(*(_history(r) for r in managers))

    async def _picks(entry, g):
        async with sem:
            try:
                pd = await fetch_fpl(f"entry/{entry}/event/{g}/picks/")
                return (entry, g), pd.get("picks", [])
            except Exception:
                return (entry, g), None

    picks_map = dict(await asyncio.gather(
        *(_picks(row.get("entry"), g) for row, _h in hist_pairs for g in gws)
    ))

    saved = []
    for g in gws:
        mgr_list = []
        for row, hist in hist_pairs:
            ev = hist.get(g)
            if ev is None:
                continue  # manager has no entry for this gameweek (joined later)
            mgr_list.append({
                "entry": row.get("entry"),
                "manager": row.get("player_name"),
                "team": row.get("entry_name"),
                "total": ev.get("total_points"),       # cumulative through g
                "event_total": ev.get("points"),       # this gameweek's points
                "overall_rank": ev.get("overall_rank"),
                "picks": _resolve_squad(picks_map.get((row.get("entry"), g)), elements_by_id, positions_by_id),
            })
        # Reconstruct in-league rank at gameweek g by cumulative points (desc).
        mgr_list.sort(key=lambda m: (m["total"] if m["total"] is not None else -1), reverse=True)
        for i, m in enumerate(mgr_list, start=1):
            m["rank"] = i
        # Date the snapshot at the gameweek's deadline.
        dl = (events.get(g) or {}).get("deadline_time")
        snap_date = _dt.fromisoformat(dl.replace("Z", "+00:00")).date() if dl else date.today()
        snapshot = {"gameweek": g, "league_name": league_name, "managers": mgr_list}
        await asyncio.to_thread(save_snapshot, int(league_id), snap_date, g, league_name, snapshot)
        saved.append({"gameweek": g, "date": snap_date.isoformat(), "managers": len(mgr_list)})

    return {"league_id": league_id, "league_name": league_name, "gameweeks": saved}


async def collect_league_history(league_id: int, snapshot_date=None) -> bool:
    """Build and persist today's league-history snapshot for ``league_id``. Returns True when
    a snapshot was saved, False when skipped (no DB, no gameweek). Best-effort: exceptions are
    logged and swallowed (returns False)."""
    from datetime import date as _date
    try:
        from mcp_fpl.db import connection as dbconn
        if not dbconn.is_configured():
            logger.info("FPL history collect skipped: no DATABASE_URL configured")
            return False
        from mcp_fpl.db.history_repo import save_snapshot
        bootstrap = await fetch_fpl_bootstrap()
        gw = resolve_gameweek(bootstrap, None, prefer="current")
        if gw is None:
            logger.info("FPL history collect skipped: no current gameweek")
            return False
        snapshot = await build_league_history_snapshot(league_id, gw, bootstrap)
        day = snapshot_date or _date.today()
        await asyncio.to_thread(
            save_snapshot, int(league_id), day, gw, snapshot.get("league_name"), snapshot
        )
        return True
    except Exception:
        logger.exception("FPL history collect failed for league %s", league_id)
        return False


def load_league_history(league_id: int, start=None, end=None) -> list:
    """Read stored daily snapshots for ``league_id`` between ``start`` and ``end`` (dates),
    oldest first, as ``{date, gameweek, data}`` dicts. Best-effort: returns [] when no DB is
    configured or on any error."""
    try:
        from mcp_fpl.db import connection as dbconn
        if not dbconn.is_configured():
            return []
        from mcp_fpl.db.history_repo import read_snapshots
        return read_snapshots(int(league_id), start, end)
    except Exception as e:  # pragma: no cover - defensive; DB is optional
        logger.warning("FPL history read failed (%s)", e)
        return []


def index_bootstrap(bootstrap: dict) -> tuple[dict, dict, dict]:
    """Return (teams_by_id, elements_by_id, positions_by_id) lookups keyed by the
    numeric ids used throughout the FPL API."""
    teams = {t["id"]: t for t in bootstrap.get("teams", [])}
    elements = {e["id"]: e for e in bootstrap.get("elements", [])}
    positions = {p["id"]: p for p in bootstrap.get("element_types", [])}
    return teams, elements, positions


def resolve_gameweek(bootstrap: dict, gameweek: Optional[int], prefer: Literal["current", "next"] = "current") -> Optional[int]:
    """Resolve a concrete gameweek id. If `gameweek` is given, return it as-is.
    Otherwise fall back to the current (or next) gameweek from the events list.
    Returns None only if the events list is empty."""
    if gameweek is not None:
        return int(gameweek)
    events = bootstrap.get("events", [])
    current = next((e for e in events if e.get("is_current")), None)
    nxt = next((e for e in events if e.get("is_next")), None)
    chosen = (current or nxt) if prefer == "current" else (nxt or current)
    return chosen["id"] if chosen else None


def gameweek_calendar(bootstrap: Optional[dict]) -> Optional[dict]:
    """A compact view of where the FPL season is: the current and next gameweek with their
    deadlines and whether the current one is finished. None when no bootstrap is at hand."""
    if not bootstrap:
        return None
    events = bootstrap.get("events", []) or []
    current = next((e for e in events if e.get("is_current")), None)
    nxt = next((e for e in events if e.get("is_next")), None)

    def _gw(e: Optional[dict]) -> Optional[dict]:
        if not e:
            return None
        return {"gameweek": e.get("id"), "deadline_time": e.get("deadline_time"),
                "finished": bool(e.get("finished"))}

    return {"current": _gw(current), "next": _gw(nxt)}


def fpl_error(message: str, exc: BaseException, *, bootstrap: Optional[dict] = None,
              gameweek: Optional[int] = None) -> dict:
    """Turn a failed FPL call into a tool result the model can act on.

    Logs the failure (an expected HTTP 4xx as a one-line warning, anything else with a
    traceback) and returns ``Message``/``Error`` plus a ``Hint`` explaining what most
    likely went wrong and how to recover — e.g. that a 404 on a gameweek's picks/live data
    usually means that gameweek hasn't reached its deadline yet — together with the current
    FPL ``gameweek_calendar`` when available, so the agent can re-orient itself instead of
    guessing. The gameweek is never changed silently: the agent decides whether to retry."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None and 400 <= status < 500:
        logger.warning("%s: %s", message, exc)
    else:
        logger.error(message, exc_info=exc)

    if status == 404 and gameweek is not None:
        hint = (
            f"FPL returned 404 for gameweek {gameweek}. Most likely that gameweek's deadline "
            "has not passed yet — squads (picks) and live points are only published after the "
            "deadline — or the gameweek number is wrong. Compare with gameweek_calendar: if "
            f"gameweek {gameweek} is the upcoming one, the latest squad available is the current "
            "gameweek's. Retry at most once with the right gameweek; if the id is wrong instead, "
            "say so."
        )
    elif status == 404:
        hint = ("FPL returned 404 — the requested manager/league/player id most likely doesn't "
                "exist or isn't public. Check the id; don't retry the same request.")
    elif status == 429:
        hint = "FPL is rate-limiting requests. Don't retry now; report the data as temporarily unavailable."
    elif (status is not None and status >= 500) or isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        hint = ("The FPL API is temporarily unavailable (it is often down while a gameweek is "
                "being updated). Don't retry repeatedly; report the data as unavailable for now.")
    else:
        hint = "Unexpected error while processing FPL data. Don't retry the same call; report it as unavailable."

    out = {"Message": message, "Error": str(exc), "Hint": hint}
    calendar = gameweek_calendar(bootstrap)
    if calendar:
        out["gameweek_calendar"] = calendar
    return out


def relevant_gameweek(bootstrap: dict) -> Optional[int]:
    """The gameweek whose matches are 'in play' for questions like "today's / the
    upcoming matches". While the current gameweek is still running (is_current and not
    finished) its round is what "today" and "the nearest matches" belong to, so return
    it; once it has finished, the round in play is the next gameweek. Falls back to the
    current gameweek if there is no next one (end of season)."""
    events = bootstrap.get("events", [])
    current = next((e for e in events if e.get("is_current")), None)
    nxt = next((e for e in events if e.get("is_next")), None)
    if current and not current.get("finished"):
        return current["id"]
    if nxt:
        return nxt["id"]
    return current["id"] if current else None


# Fixture "stats" identifiers we surface as match events, mapped to friendlier keys.
_FIXTURE_EVENT_IDENTIFIERS = {
    "goals_scored": "goals",
    "assists": "assists",
    "own_goals": "own_goals",
    "yellow_cards": "yellow_cards",
    "red_cards": "red_cards",
    "penalties_missed": "penalties_missed",
    "penalties_saved": "penalties_saved",
}


def summarize_fixture_events(fixture: dict, elements_by_id: dict, teams_by_id: dict) -> dict:
    """Turn a fixture's raw `stats` array into name-based match events (who scored,
    assisted, got booked, etc.). Each event lists the player's web name and team short
    code, resolved from the numeric element/team ids."""
    home = teams_by_id.get(fixture.get("team_h"), {})
    away = teams_by_id.get(fixture.get("team_a"), {})
    events: dict = {}
    for stat in fixture.get("stats", []):
        key = _FIXTURE_EVENT_IDENTIFIERS.get(stat.get("identifier"))
        if not key:
            continue
        entries = []
        for side, team in (("h", home), ("a", away)):
            for item in stat.get(side, []):
                element = elements_by_id.get(item.get("element"), {})
                entries.append({
                    "player": element.get("web_name"),
                    "team": team.get("short_name"),
                    "count": item.get("value"),
                })
        if entries:
            events[key] = entries
    return events


def find_players_by_name(bootstrap: dict, query: str) -> list[dict]:
    """Resolve a free-text player name (e.g. 'Haaland', 'Dalot', 'Bruno Fernandes')
    to matching element records, best match first. An exact web-name match wins and is
    returned alone; otherwise all substring matches (across web/first/second name) are
    returned so the caller can disambiguate."""
    q = (query or "").strip().lower()
    if not q:
        return []
    exact, partial = [], []
    for e in bootstrap.get("elements", []):
        web = (e.get("web_name") or "").lower()
        haystack = " ".join([web, (e.get("first_name") or ""), (e.get("second_name") or "")]).lower()
        if web == q:
            exact.append(e)
        elif q in haystack:
            partial.append(e)
    return exact or partial


def describe_player(element: dict, teams_by_id: dict, positions_by_id: dict) -> dict:
    """Flatten one 'element' (player) record into a compact, name-based summary."""
    team = teams_by_id.get(element.get("team"), {})
    position = positions_by_id.get(element.get("element_type"), {})
    return {
        "name": element.get("web_name"),
        "full_name": f"{element.get('first_name', '')} {element.get('second_name', '')}".strip(),
        "team": team.get("name"),
        "team_short": team.get("short_name"),
        "position": position.get("singular_name_short"),
        "price": round(element.get("now_cost", 0) / 10, 1),  # FPL prices are in tenths of £m
        "total_points": element.get("total_points"),
        "form": element.get("form"),
        "selected_by_percent": element.get("selected_by_percent"),
        # status 'a'=available, 'i'=injured, 'd'=doubtful, 's'=suspended, 'u'=unavailable
        "status": element.get("status"),
        "news": element.get("news") or None,
    }

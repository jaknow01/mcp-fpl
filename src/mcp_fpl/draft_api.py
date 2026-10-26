"""FPL Draft API helpers (https://draft.premierleague.com/api/), the draft counterpart of
fpl_api: keyless fetch, cached reference data, and the translations the tools need.

Draft is a separate game with its own ids:
- managers ("entries") and leagues are a separate namespace from classic FPL;
- a league's table and H2H matches refer to managers by ``league_entry`` (the
  ``league_entries[].id``), while ownership, transactions and draft picks use ``entry_id``;
- most player ("element") ids match classic FPL, but players added mid-season do not.
  The stable link between the two games is the player's ``code``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Union

import requests

from mcp_fpl.fpl_api import FPL_HEADERS, _fpl_db_ttl

logger = logging.getLogger(__name__)

DRAFT_BASE = "https://draft.premierleague.com/api"
_DRAFT_BOOTSTRAP_CACHE_ID = "draft_bootstrap"

# Short in-process memos: the bootstrap changes ~daily; league state (owners, table)
# changes with waivers/trades, so it is kept for a minute only.
_L1_BOOTSTRAP_TTL = 300
_L1_LEAGUE_TTL = 60
_memo: dict = {}


async def fetch_draft(path: str) -> Union[dict, list]:
    """GET a JSON document from the draft API. Raises on HTTP errors."""
    url = f"{DRAFT_BASE}/{path.lstrip('/')}"

    def _do():
        resp = requests.get(url, headers=FPL_HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json()

    return await asyncio.to_thread(_do)


async def _memoized(key: str, ttl: int, path: str, force: bool = False):
    now = time.time()
    hit = _memo.get(key)
    if not force and hit is not None and now - hit[1] < ttl:
        return hit[0]
    data = await fetch_draft(path)
    _memo[key] = (data, now)
    return data


def _load_from_db(max_age: int) -> Optional[dict]:
    try:
        from mcp_fpl.db import connection as dbconn
        if not dbconn.is_configured():
            return None
        from mcp_fpl.db.reference_repo import get_cached_bootstrap
        return get_cached_bootstrap(max_age, _DRAFT_BOOTSTRAP_CACHE_ID)
    except Exception as e:  # pragma: no cover - defensive; DB is optional
        logger.warning("Draft bootstrap DB cache read failed (%s); using API", e)
        return None


def _save_to_db(data: dict) -> None:
    try:
        from mcp_fpl.db import connection as dbconn
        if not dbconn.is_configured():
            return
        from mcp_fpl.db.reference_repo import save_bootstrap
        save_bootstrap(data, _DRAFT_BOOTSTRAP_CACHE_ID)
    except Exception as e:  # pragma: no cover - defensive; DB is optional
        logger.warning("Draft bootstrap DB cache write failed (%s)", e)


async def fetch_draft_bootstrap(force: bool = False) -> dict:
    """Draft reference data (players, teams, positions, gameweeks): memory → Postgres → API."""
    now = time.time()
    hit = _memo.get("bootstrap")
    if not force and hit is not None and now - hit[1] < _L1_BOOTSTRAP_TTL:
        return hit[0]
    if not force:
        cached = await asyncio.to_thread(_load_from_db, _fpl_db_ttl())
        if cached is not None:
            _memo["bootstrap"] = (cached, now)
            return cached
    data = await fetch_draft("bootstrap-static")
    _memo["bootstrap"] = (data, now)
    await asyncio.to_thread(_save_to_db, data)
    return data


async def fetch_draft_game() -> dict:
    """Where the draft season is: current/next gameweek and whether the current one is over."""
    return await _memoized("game", _L1_LEAGUE_TTL, "game")


async def fetch_draft_league(league_id: int) -> dict:
    """``league/{id}/details``: the league, its managers, table and (H2H) matches."""
    return await _memoized(f"details:{league_id}", _L1_LEAGUE_TTL, f"league/{int(league_id)}/details")


async def fetch_draft_element_status(league_id: int) -> list[dict]:
    """Who owns each player in a draft league (``owner`` is an entry_id, None = unowned)."""
    data = await _memoized(f"status:{league_id}", _L1_LEAGUE_TTL, f"league/{int(league_id)}/element-status")
    return data.get("element_status", []) or []


def draft_gameweek(game: dict, gameweek: Optional[int] = None, prefer: str = "current") -> Optional[int]:
    """A concrete gameweek: the given one, else the current one; with ``prefer="upcoming"``
    the next gameweek once the current one has finished."""
    if gameweek is not None:
        return int(gameweek)
    current, nxt = game.get("current_event"), game.get("next_event")
    if prefer == "upcoming" and game.get("current_event_finished") and nxt:
        return int(nxt)
    return int(current) if current else (int(nxt) if nxt else None)


# ── indexes and translations ─────────────────────────────────────────────────

def index_draft_bootstrap(bootstrap: dict) -> tuple[dict, dict, dict]:
    """(teams_by_id, elements_by_id, positions_by_id) keyed by draft ids."""
    teams = {t["id"]: t for t in bootstrap.get("teams", [])}
    elements = {e["id"]: e for e in bootstrap.get("elements", [])}
    positions = {p["id"]: p for p in bootstrap.get("element_types", [])}
    return teams, elements, positions


def classic_ids_by_draft_id(draft_bootstrap: dict, classic_bootstrap: dict) -> dict[int, int]:
    """Draft element id → classic element id, matched on the player's ``code``."""
    classic_by_code = {e.get("code"): e["id"] for e in classic_bootstrap.get("elements", [])}
    out = {}
    for e in draft_bootstrap.get("elements", []):
        cid = classic_by_code.get(e.get("code"))
        if cid is not None:
            out[e["id"]] = cid
    return out


def describe_draft_player(element: dict, teams_by_id: dict, positions_by_id: dict) -> dict:
    """A compact, name-based summary of a draft player (no prices: draft has none)."""
    team = teams_by_id.get(element.get("team"), {})
    position = positions_by_id.get(element.get("element_type"), {})
    return {
        "name": element.get("web_name"),
        "full_name": f"{element.get('first_name', '')} {element.get('second_name', '')}".strip(),
        "team": team.get("name"),
        "team_short": team.get("short_name"),
        "position": position.get("singular_name_short"),
        "total_points": element.get("total_points"),
        "form": element.get("form"),
        # status 'a'=available, 'i'=injured, 'd'=doubtful, 's'=suspended, 'u'=unavailable
        "status": element.get("status"),
        "news": element.get("news") or None,
    }


class DraftLeague:
    """One draft league's details with its managers indexed both ways."""

    def __init__(self, details: dict):
        self.details = details
        self.league = details.get("league", {}) or {}
        self.entries = details.get("league_entries", []) or []
        self.by_league_entry = {e.get("id"): e for e in self.entries}
        self.by_entry_id = {e.get("entry_id"): e for e in self.entries if e.get("entry_id") is not None}

    @property
    def name(self) -> Optional[str]:
        return self.league.get("name")

    @property
    def is_h2h(self) -> bool:
        return self.league.get("scoring") == "h"

    @property
    def transaction_mode(self) -> Optional[str]:
        return self.league.get("transaction_mode")

    @staticmethod
    def manager_name(entry: dict) -> str:
        return f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}".strip()

    def describe(self, entry: Optional[dict]) -> dict:
        if not entry:
            return {"manager": None, "team_name": None, "entry_id": None}
        return {"manager": self.manager_name(entry), "team_name": entry.get("entry_name"),
                "entry_id": entry.get("entry_id")}

    def by_entry(self, entry_id) -> dict:
        return self.describe(self.by_entry_id.get(entry_id))

    def by_league_entry_id(self, league_entry_id) -> dict:
        return self.describe(self.by_league_entry.get(league_entry_id))

    def find_manager(self, query: str) -> list[dict]:
        """Managers whose name or team name contains ``query`` (case-insensitive)."""
        q = (query or "").strip().lower()
        if not q:
            return []
        if q.isdigit() and int(q) in self.by_entry_id:
            return [self.by_entry_id[int(q)]]
        return [e for e in self.entries
                if q in self.manager_name(e).lower() or q in (e.get("entry_name") or "").lower()]


async def list_draft_league_managers(league_id: int, max_managers: int = 500):
    """``(league_name, rows, complete)`` in the same row shape the /join roster uses for
    classic leagues (``entry``, ``entry_name``, ``player_name``)."""
    league = DraftLeague(await fetch_draft_league(league_id))
    rows = [{"entry": e.get("entry_id"), "entry_name": e.get("entry_name"),
             "player_name": DraftLeague.manager_name(e)}
            for e in league.entries if e.get("entry_id") is not None]
    return league.name, rows[:max_managers], len(rows) <= max_managers


TRANSACTION_KINDS = {"w": "waiver", "f": "free agent"}
TRANSACTION_RESULTS = {"a": "accepted", "do": "denied (outbid by a higher waiver priority)",
                       "di": "denied (invalid — player no longer available or squad rules)"}
TRADE_STATES = {"o": "offered", "a": "accepted (awaiting processing)", "p": "processed",
                "r": "rejected", "v": "vetoed", "c": "cancelled", "e": "expired", "i": "invalid"}

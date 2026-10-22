"""Postgres-backed cache for Fantasy Premier League mini-league squad snapshots.

Answering "who else in my league has player X" or "how many own a player from team Y"
means fetching every manager's picks for a gameweek — dozens of calls to the unofficial
FPL API — and the owner asks these repeatedly. Those picks are frozen once the gameweek's
deadline has passed, so we snapshot a whole league+gameweek scan here as one JSONB row and
serve it back instead of re-scanning every turn.

``mcp_fpl.fpl_api.load_league_squads`` is the caller: it reads through this
cache when a database is configured and writes a fresh snapshot back after an API scan.

Best-effort by design — the database is optional; callers swallow errors and fall back to
the live API when it is not available (mirrors ``fpl_repo``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from psycopg.types.json import Json

from mcp_fpl.db.connection import connection
from mcp_fpl.db.schema import init_db

logger = logging.getLogger(__name__)

_schema_ready = False


def _ensure_schema() -> None:
    """Create the tables on first use (idempotent). Cheap after the first call."""
    global _schema_ready
    if not _schema_ready:
        init_db()
        _schema_ready = True


def get_cached_league_squads(
    league_id: int, gameweek: int, max_age_seconds: int, min_managers: int
) -> Optional[dict]:
    """Return the cached squad snapshot for ``(league_id, gameweek)`` if it is present,
    younger than ``max_age_seconds`` and covers enough managers for the request; otherwise
    None (signalling the caller to refresh from the API).

    "Covers enough" means the snapshot captured the whole league (``complete``) or already
    scanned at least ``min_managers`` — so a scan asking for no more than was cached is served
    from Postgres, while one asking for more than a partial snapshot holds falls through to a
    fresh API scan.

    The returned dict has ``league_name``, ``max_managers``, ``complete`` and ``pairs`` — a
    list of ``[standings_row, picks]`` two-item lists (picks is None for a manager whose squad
    could not be fetched), matching the in-memory shape produced by the API scan."""
    _ensure_schema()
    with connection() as conn:
        row = conn.execute(
            """
            SELECT league_name, max_managers, complete, data, fetched_at
            FROM fpl_league_squads WHERE league_id = %s AND gameweek = %s
            """,
            (league_id, gameweek),
        ).fetchone()
    if not row:
        return None
    league_name, cached_max, complete, data, fetched_at = row
    age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
    if age > max_age_seconds:
        logger.info(
            "FPL league %s GW%s squads cache is stale (%.0fs old); refreshing from API",
            league_id, gameweek, age,
        )
        return None
    if not complete and (cached_max or 0) < min_managers:
        logger.info(
            "FPL league %s GW%s squads cache covers %s managers (< requested %s) and is "
            "incomplete; refreshing from API", league_id, gameweek, cached_max, min_managers,
        )
        return None
    logger.info(
        "FPL league %s GW%s squads served from Postgres cache (%.0fs old)",
        league_id, gameweek, age,
    )
    if not isinstance(data, (dict, list)):
        import json
        data = json.loads(data)
    return {
        "league_name": league_name,
        "max_managers": cached_max,
        "complete": complete,
        "pairs": data.get("pairs", []) if isinstance(data, dict) else [],
    }


def save_league_squads(
    league_id: int,
    gameweek: int,
    league_name: Optional[str],
    max_managers: int,
    complete: bool,
    pairs: list,
) -> None:
    """Upsert a freshly scanned league+gameweek squad snapshot, stamping the fetch time.

    ``pairs`` is a list of ``[standings_row, picks]`` two-item lists (JSON-serializable)."""
    _ensure_schema()
    now = datetime.now(timezone.utc)
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO fpl_league_squads
                (league_id, gameweek, league_name, max_managers, complete, data, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (league_id, gameweek) DO UPDATE SET
                league_name = EXCLUDED.league_name,
                max_managers = EXCLUDED.max_managers,
                complete = EXCLUDED.complete,
                data = EXCLUDED.data,
                fetched_at = EXCLUDED.fetched_at
            """,
            (
                league_id, gameweek, league_name, max_managers, complete,
                Json({"pairs": pairs}), now,
            ),
        )
    logger.info(
        "FPL league %s GW%s squads cached to Postgres (%d managers, complete=%s)",
        league_id, gameweek, len(pairs), complete,
    )

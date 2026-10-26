"""Postgres-backed cache for Fantasy Premier League reference data.

The FPL ``bootstrap-static`` document (teams, players/"elements", positions and
gameweeks) only changes ~daily, so we keep the latest snapshot here as a single JSONB
row and serve it from the database instead of refetching it from the unofficial API on
every turn. ``mcp_fpl.fpl_api.fetch_fpl_bootstrap`` is the caller: it reads through this
cache when a database is configured and writes a fresh snapshot back after an API fetch.

Best-effort by design — the database is optional; callers swallow errors and fall back
to the live API when it is not available.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from psycopg.types.json import Json

from mcp_fpl.db.connection import connection
from mcp_fpl.db.schema import init_db

logger = logging.getLogger(__name__)

# Single logical snapshot; keyed so future per-league/per-entry caches can share the
# table with distinct ids if ever needed.
_BOOTSTRAP_ID = "bootstrap"

_schema_ready = False


def _ensure_schema() -> None:
    """Create the tables on first use (idempotent). Cheap after the first call."""
    global _schema_ready
    if not _schema_ready:
        init_db()
        _schema_ready = True


def get_cached_bootstrap(max_age_seconds: int, cache_id: str = _BOOTSTRAP_ID) -> Optional[dict]:
    """Return the cached bootstrap snapshot if present and younger than
    ``max_age_seconds``; otherwise None (signalling the caller to refresh from the API).
    ``cache_id`` keeps the classic and draft bootstraps apart in the same table."""
    _ensure_schema()
    with connection() as conn:
        row = conn.execute(
            "SELECT data, fetched_at FROM fpl_reference WHERE id = %s",
            (cache_id,),
        ).fetchone()
    if not row:
        return None
    data, fetched_at = row
    age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
    if age > max_age_seconds:
        logger.info("FPL %s cache is stale (%.0fs old); refreshing from API", cache_id, age)
        return None
    logger.info("FPL %s served from Postgres cache (%.0fs old)", cache_id, age)
    # JSONB comes back already decoded by psycopg; be tolerant just in case.
    if isinstance(data, (dict, list)):
        return data
    import json
    return json.loads(data)


def save_bootstrap(data: dict, cache_id: str = _BOOTSTRAP_ID) -> None:
    """Upsert the freshly fetched bootstrap snapshot, stamping the fetch time."""
    _ensure_schema()
    now = datetime.now(timezone.utc)
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO fpl_reference (id, data, fetched_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                data = EXCLUDED.data, fetched_at = EXCLUDED.fetched_at
            """,
            (cache_id, Json(data), now),
        )
    logger.info("FPL %s cached to Postgres (%d players)", cache_id, len(data.get("elements", [])))

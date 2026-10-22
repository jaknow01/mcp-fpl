"""Postgres-backed permanent daily history of a FPL mini-league.

Distinct from ``fpl_league_repo`` (the TTL squad *cache*, overwritten per gameweek): this is
append-only history, one snapshot per ``(league_id, snapshot_date)`` capturing every
manager's standing (rank + points) AND their squad (which footballers they had) for that
day's current gameweek. Written daily at 04:00 by the FPL collector
(``mcp-fpl collect``); read by the get_league_history tool.

Best-effort by design — the database is optional; callers select the backend up front.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from psycopg.types.json import Json

from mcp_fpl.db.connection import connection
from mcp_fpl.db.schema import init_db

logger = logging.getLogger(__name__)

_schema_ready = False


def _ensure_schema() -> None:
    global _schema_ready
    if not _schema_ready:
        init_db()
        _schema_ready = True


def save_snapshot(
    league_id: int,
    snapshot_date: date,
    gameweek: Optional[int],
    league_name: Optional[str],
    data: dict,
) -> None:
    """Upsert one daily league snapshot. ``data`` is the JSON-serializable snapshot body
    (``{gameweek, league_name, managers:[...]}``)."""
    _ensure_schema()
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO fpl_league_history
                (league_id, snapshot_date, gameweek, league_name, data, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (league_id, snapshot_date) DO UPDATE SET
                gameweek = EXCLUDED.gameweek,
                league_name = EXCLUDED.league_name,
                data = EXCLUDED.data,
                fetched_at = EXCLUDED.fetched_at
            """,
            (
                league_id, snapshot_date, gameweek, league_name,
                Json(data), datetime.now(timezone.utc),
            ),
        )
    logger.info(
        "FPL league %s history snapshot saved for %s (GW%s, %d managers)",
        league_id, snapshot_date, gameweek, len(data.get("managers", [])),
    )


def get_snapshot(league_id: int, snapshot_date: date) -> Optional[dict]:
    """Return the snapshot body for a specific day, or None."""
    _ensure_schema()
    with connection() as conn:
        row = conn.execute(
            "SELECT data FROM fpl_league_history WHERE league_id = %s AND snapshot_date = %s",
            (league_id, snapshot_date),
        ).fetchone()
    if not row:
        return None
    data = row[0]
    if not isinstance(data, (dict, list)):
        import json
        data = json.loads(data)
    return data


def read_snapshots(
    league_id: int, start: Optional[date] = None, end: Optional[date] = None
) -> list[dict]:
    """Return snapshots for ``league_id`` between ``start`` and ``end`` (inclusive), oldest
    first, as ``{date, gameweek, data}`` dicts."""
    _ensure_schema()
    clauses = ["league_id = %s"]
    params: list = [league_id]
    if start is not None:
        clauses.append("snapshot_date >= %s")
        params.append(start)
    if end is not None:
        clauses.append("snapshot_date <= %s")
        params.append(end)
    sql = (
        "SELECT snapshot_date, gameweek, data FROM fpl_league_history WHERE "
        + " AND ".join(clauses)
        + " ORDER BY snapshot_date ASC"
    )
    with connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    out = []
    for d, gw, data in rows:
        if not isinstance(data, (dict, list)):
            import json
            data = json.loads(data)
        # Normalize the DATE to an ISO string so the tool output serializes cleanly for the LLM.
        out.append({"date": d.isoformat() if hasattr(d, "isoformat") else d, "gameweek": gw, "data": data})
    return out


def latest_date(league_id: int) -> Optional[date]:
    """Most recent snapshot date stored for ``league_id``, or None."""
    _ensure_schema()
    with connection() as conn:
        row = conn.execute(
            "SELECT max(snapshot_date) FROM fpl_league_history WHERE league_id = %s",
            (league_id,),
        ).fetchone()
    return row[0] if row else None

"""Postgres-backed store for self-service MCP API keys (table ``mcp_api_keys``).

``mcp_fpl.keys`` is the caller: it uses this repo when a database is configured and
falls back to the on-disk JSON store otherwise (same pattern as the other stores). Only
SHA-256 key hashes are stored.
"""
from __future__ import annotations

from typing import Optional

from psycopg.types.json import Json

from mcp_fpl.db.connection import connection
from mcp_fpl.db.schema import init_db

_schema_ready = False


def _ensure_schema() -> None:
    global _schema_ready
    if not _schema_ready:
        init_db()
        _schema_ready = True


_COLUMNS = "key_sha256, entry_id, league_id, manager_name, team_name, servers, created_at, revoked_at"


def _row(r) -> dict:
    return {
        "key_sha256": r[0], "entry_id": r[1], "league_id": r[2], "manager_name": r[3],
        "team_name": r[4], "servers": list(r[5] or []),
        "created_at": r[6].isoformat() if r[6] else None,
        "revoked_at": r[7].isoformat() if r[7] else None,
    }


def issue(*, key_sha256: str, entry_id: int, league_id: Optional[int], manager_name: str,
          team_name: str, servers: list[str]) -> int:
    """Revoke the entry's active keys and insert the new one, atomically. Returns how many
    keys were revoked (0 = first key for this entry)."""
    _ensure_schema()
    with connection() as conn:
        cur = conn.execute(
            "UPDATE mcp_api_keys SET revoked_at = now() WHERE entry_id = %s AND revoked_at IS NULL",
            (entry_id,),
        )
        replaced = cur.rowcount or 0
        conn.execute(
            "INSERT INTO mcp_api_keys (key_sha256, entry_id, league_id, manager_name, team_name, servers)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (key_sha256, entry_id, league_id, manager_name, team_name, Json(servers)),
        )
    return replaced


def active() -> list[dict]:
    _ensure_schema()
    with connection() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM mcp_api_keys WHERE revoked_at IS NULL ORDER BY created_at"
        ).fetchall()
    return [_row(r) for r in rows]


def revoke_entry(entry_id: int) -> int:
    _ensure_schema()
    with connection() as conn:
        cur = conn.execute(
            "UPDATE mcp_api_keys SET revoked_at = now() WHERE entry_id = %s AND revoked_at IS NULL",
            (entry_id,),
        )
        return cur.rowcount or 0

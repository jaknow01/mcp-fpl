"""Postgres-backed store for self-service MCP API keys (table ``mcp_api_keys``).

``mcp_fpl.keys`` is the caller: it uses this repo when a database is configured and
falls back to the on-disk JSON store otherwise (same pattern as the other stores). Only
SHA-256 key hashes are stored. A key links a classic FPL team (``entry_id``), an FPL Draft
team (``draft_entry_id``), or both.
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


_COLUMNS = ("key_sha256, entry_id, league_id, manager_name, team_name, servers, created_at, revoked_at, "
            "draft_entry_id, draft_league_id, draft_team_name")

# Active keys sharing the classic team or the draft team (a NULL id never matches).
_SAME_PERSON = ("revoked_at IS NULL AND ((entry_id IS NOT NULL AND entry_id = %s) "
                "OR (draft_entry_id IS NOT NULL AND draft_entry_id = %s))")


def _row(r) -> dict:
    return {
        "key_sha256": r[0], "entry_id": r[1], "league_id": r[2], "manager_name": r[3],
        "team_name": r[4], "servers": list(r[5] or []),
        "created_at": r[6].isoformat() if r[6] else None,
        "revoked_at": r[7].isoformat() if r[7] else None,
        "draft_entry_id": r[8], "draft_league_id": r[9], "draft_team_name": r[10],
    }


def issue(*, key_sha256: str, entry_id: Optional[int] = None, league_id: Optional[int] = None,
          draft_entry_id: Optional[int] = None, draft_league_id: Optional[int] = None,
          manager_name: str = "", team_name: Optional[str] = None,
          draft_team_name: Optional[str] = None, servers: list[str]) -> int:
    """Revoke every active key that shares the classic or the draft team, then insert the
    new one, atomically. Returns how many keys were revoked (0 = first key)."""
    if entry_id is None and draft_entry_id is None:
        raise ValueError("a key needs a classic or a draft team")
    _ensure_schema()
    with connection() as conn:
        cur = conn.execute(f"UPDATE mcp_api_keys SET revoked_at = now() WHERE {_SAME_PERSON}",
                           (entry_id, draft_entry_id))
        replaced = cur.rowcount or 0
        conn.execute(
            "INSERT INTO mcp_api_keys (key_sha256, entry_id, league_id, manager_name, team_name, servers,"
            " draft_entry_id, draft_league_id, draft_team_name)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (key_sha256, entry_id, league_id, manager_name, team_name, Json(servers),
             draft_entry_id, draft_league_id, draft_team_name),
        )
    return replaced


def active() -> list[dict]:
    _ensure_schema()
    with connection() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM mcp_api_keys WHERE revoked_at IS NULL ORDER BY created_at"
        ).fetchall()
    return [_row(r) for r in rows]


def revoke_entry(entry_id: int, game: str = "classic") -> int:
    """Revoke the active key(s) of a classic team (``game="classic"``) or a draft team."""
    _ensure_schema()
    column = "draft_entry_id" if game == "draft" else "entry_id"
    with connection() as conn:
        cur = conn.execute(
            f"UPDATE mcp_api_keys SET revoked_at = now() WHERE {column} = %s AND revoked_at IS NULL",
            (entry_id,),
        )
        return cur.rowcount or 0

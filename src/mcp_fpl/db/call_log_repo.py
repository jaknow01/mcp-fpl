"""Postgres store for the tool-call log (table ``fpl_tool_calls``); ``mcp_fpl.call_log`` is
the caller. Writes are best-effort and happen off the request path."""
from __future__ import annotations

from psycopg.types.json import Json

from mcp_fpl.db.connection import connection
from mcp_fpl.db.schema import init_db

_schema_ready = False


def _ensure_schema() -> None:
    global _schema_ready
    if not _schema_ready:
        init_db()
        _schema_ready = True


def record(row: dict) -> None:
    _ensure_schema()
    with connection() as conn:
        conn.execute(
            "INSERT INTO fpl_tool_calls (client_id, principal, tool, arguments, outcome, error,"
            " duration_ms, result_chars, version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (row.get("client_id"), row.get("principal"), row["tool"], Json(row.get("arguments")),
             row["outcome"], row.get("error"), row.get("duration_ms"), row.get("result_chars"),
             row.get("version")),
        )


def purge(older_than_days: int) -> int:
    """Delete calls older than ``older_than_days``; returns how many."""
    _ensure_schema()
    with connection() as conn:
        cur = conn.execute("DELETE FROM fpl_tool_calls WHERE called_at < now() - make_interval(days => %s)",
                           (int(older_than_days),))
        return cur.rowcount or 0


def summary(days: int) -> list[dict]:
    """Per tool over the last ``days``: calls, distinct clients, failures, timing."""
    _ensure_schema()
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT tool, count(*), count(DISTINCT client_id),
                   count(*) FILTER (WHERE outcome <> 'ok'),
                   round(avg(duration_ms)), max(duration_ms)
            FROM fpl_tool_calls
            WHERE called_at >= now() - make_interval(days => %s)
            GROUP BY tool ORDER BY count(*) DESC
            """,
            (int(days),),
        ).fetchall()
    return [{"tool": r[0], "calls": r[1], "clients": r[2], "not_ok": r[3],
             "avg_ms": int(r[4]) if r[4] is not None else None, "max_ms": r[5]} for r in rows]

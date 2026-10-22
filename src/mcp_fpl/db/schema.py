"""mcp-fpl database schema (its own database; created idempotently on first use)."""
from __future__ import annotations

import logging

from mcp_fpl.db.connection import connection

logger = logging.getLogger(__name__)

SCHEMA_STATEMENTS = [
    # bootstrap-static snapshot (teams, players, positions, gameweeks), cached with a timestamp
    """
    CREATE TABLE IF NOT EXISTS fpl_reference (
        id         TEXT PRIMARY KEY,
        data       JSONB NOT NULL,
        fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # mini-league squad scans per (league, gameweek) — the slow "who owns X" path, TTL-refreshed
    """
    CREATE TABLE IF NOT EXISTS fpl_league_squads (
        league_id    BIGINT NOT NULL,
        gameweek     INTEGER NOT NULL,
        league_name  TEXT,
        max_managers INTEGER NOT NULL,
        complete     BOOLEAN NOT NULL DEFAULT FALSE,
        data         JSONB NOT NULL,
        fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (league_id, gameweek)
    )
    """,
    # permanent daily league snapshots (rank, points, squad per manager)
    """
    CREATE TABLE IF NOT EXISTS fpl_league_history (
        league_id     BIGINT NOT NULL,
        snapshot_date DATE NOT NULL,
        gameweek      INTEGER,
        league_name   TEXT,
        data          JSONB NOT NULL,
        fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (league_id, snapshot_date)
    )
    """,
    # self-service API keys from /mcp/join (sha256 only; one active per entry)
    """
    CREATE TABLE IF NOT EXISTS mcp_api_keys (
        key_sha256   TEXT PRIMARY KEY,
        entry_id     BIGINT NOT NULL,
        league_id    BIGINT,
        manager_name TEXT,
        team_name    TEXT,
        servers      JSONB NOT NULL DEFAULT '["fpl"]'::jsonb,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        revoked_at   TIMESTAMPTZ
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS mcp_api_keys_active_by_entry
        ON mcp_api_keys (entry_id) WHERE revoked_at IS NULL
    """,
]


def init_db() -> None:
    with connection() as conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
    logger.info("Database schema ensured")

"""One-shot jobs for CronJobs / cron: daily league snapshot, cache warming, backfill.

Each runs for every league in ``FPL_LEAGUES`` (or the leagues given) and returns an exit code.
"""
from __future__ import annotations

import logging
from typing import Optional

from mcp_fpl.config import league_ids
from mcp_fpl.db import connection as dbconn
from mcp_fpl.fpl_api import (
    _LEAGUE_SCAN_DEFAULT,
    backfill_league_history,
    collect_league_history,
    fetch_fpl_bootstrap,
    load_league_squads,
    resolve_gameweek,
)

logger = logging.getLogger(__name__)


def _leagues(explicit: Optional[list[int]]) -> list[int]:
    leagues = explicit or league_ids()
    if not leagues:
        logger.error("No leagues: set FPL_LEAGUES or pass --league")
    return leagues


def purge_call_log() -> None:
    """Apply the tool-call log retention when MCP_CALL_LOG_DAYS is set (default: keep all)."""
    from mcp_fpl.call_log import retention_days
    from mcp_fpl.db.call_log_repo import purge
    days = retention_days()
    if days is None:
        return
    try:
        n = purge(days)
        logger.info("Tool-call log: removed %d call(s) older than %d days", n, days)
    except Exception:
        logger.exception("Tool-call log purge failed")


async def collect(leagues: Optional[list[int]] = None) -> int:
    """Today's history snapshot per league (Postgres only), plus the tool-call log retention
    if one is set. Exit 0 when all succeeded."""
    if not dbconn.is_configured():
        logger.error("collect needs DATABASE_URL (league history is stored in Postgres)")
        return 2
    purge_call_log()
    leagues = _leagues(leagues)
    failed = 0
    for lid in leagues:
        ok = await collect_league_history(lid)
        logger.info("League %s snapshot: %s", lid, "saved" if ok else "skipped/failed")
        failed += 0 if ok else 1
    return 1 if failed or not leagues else 0


async def warm_cache(leagues: Optional[list[int]] = None) -> int:
    """Force-refresh the current gameweek's squad scan per league."""
    leagues = _leagues(leagues)
    if not leagues:
        return 1
    bootstrap = await fetch_fpl_bootstrap()
    gw = resolve_gameweek(bootstrap, None, prefer="current")
    if gw is None:
        logger.info("No current gameweek — nothing to warm")
        return 0
    failed = 0
    for lid in leagues:
        try:
            await load_league_squads(lid, gw, _LEAGUE_SCAN_DEFAULT, force=True)
            logger.info("League %s GW%s squads cache warmed", lid, gw)
        except Exception:
            logger.exception("Warming league %s failed", lid)
            failed += 1
    return 1 if failed else 0


async def backfill(leagues: Optional[list[int]] = None, from_gw: int = 1) -> int:
    """Rebuild per-gameweek history snapshots from the FPL API."""
    if not dbconn.is_configured():
        logger.error("backfill needs DATABASE_URL (league history is stored in Postgres)")
        return 2
    leagues = _leagues(leagues)
    failed = 0
    for lid in leagues:
        result = await backfill_league_history(lid, from_gw=from_gw)
        if result.get("error"):
            logger.error("League %s backfill failed: %s", lid, result["error"])
            failed += 1
            continue
        logger.info("League %s (%s): %d gameweek snapshot(s)", lid, result.get("league_name"),
                    len(result.get("gameweeks", [])))
    return 1 if failed or not leagues else 0

"""Tool-call log: every tool call is recorded in Postgres (``fpl_tool_calls``) to see how the
tools are used and where they fail — which tools, with which arguments, the outcome and how
long it took. Only tool calls are visible to the server, never the users' conversations, and
tool results are not stored (only their size).

Best-effort and off the request path: the row is written in a background thread after the
call, and any logging failure is swallowed. Needs ``DATABASE_URL``; ``MCP_CALL_LOG=off``
turns it off. ``MCP_CALL_LOG_DAYS`` (default 180) is the retention, applied by the daily
``mcp-fpl collect`` job.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable, Optional

from jarvis_mcp_kit import current_client, current_principal

from mcp_fpl.config import env_flag, env_int

logger = logging.getLogger(__name__)

_background: set[asyncio.Task] = set()
_MAX_ERROR = 500

try:
    _VERSION: Optional[str] = version("mcp-fpl")
except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
    _VERSION = None


def retention_days() -> int:
    return env_int("MCP_CALL_LOG_DAYS", 180, minimum=1)


def enabled() -> bool:
    if not env_flag("MCP_CALL_LOG", True):
        return False
    from mcp_fpl.db import connection as dbconn
    return dbconn.is_configured()


def _write(row: dict) -> None:
    from mcp_fpl.db.call_log_repo import record
    record(row)


async def _store(row: dict) -> None:
    try:
        await asyncio.to_thread(_write, row)
    except Exception:
        logger.warning("Tool-call log write failed", exc_info=True)


def _arguments(fn: Callable, args: tuple, kwargs: dict) -> dict:
    """The call's arguments by name (positional ones included), JSON-safe. Unset (None)
    arguments are left out: the MCP layer passes every optional argument explicitly."""
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        data = dict(bound.arguments)
    except TypeError:
        data = dict(kwargs)
    return json.loads(json.dumps({k: v for k, v in data.items() if v is not None}, default=str))


def _result_info(result: Any) -> tuple[str, Optional[str], Optional[int]]:
    """(outcome, error, size): a dict with ``Error`` counts as a tool-level error — the tool
    answered, but could not do what was asked (missing id, ambiguous game, API failure…)."""
    try:
        size = len(json.dumps(result, ensure_ascii=False, default=str))
    except Exception:
        size = None
    if isinstance(result, dict) and result.get("Error"):
        return "tool_error", str(result["Error"])[:_MAX_ERROR], size
    return "ok", None, size


def recorded(fn: Callable, tool_name: str) -> Callable:
    """Wrap a tool so each call is logged. The signature (and so the tool schema) is kept."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        if not enabled():
            return await fn(*args, **kwargs)
        started = time.monotonic()
        client = current_client()
        row = {"tool": tool_name, "client_id": client.client_id if client else None,
               "principal": current_principal(), "arguments": _arguments(fn, args, kwargs),
               "version": _VERSION}
        try:
            result = await fn(*args, **kwargs)
        except asyncio.CancelledError:  # the kit's timeout cancels the call
            row.update(outcome="timeout")
            raise
        except BaseException as e:
            row.update(outcome="exception", error=f"{type(e).__name__}: {e}"[:_MAX_ERROR])
            raise
        else:
            outcome, error, size = _result_info(result)
            row.update(outcome=outcome, error=error, result_chars=size)
            return result
        finally:
            row["duration_ms"] = int((time.monotonic() - started) * 1000)
            task = asyncio.get_running_loop().create_task(_store(row))
            _background.add(task)
            task.add_done_callback(_background.discard)

    return wrapper

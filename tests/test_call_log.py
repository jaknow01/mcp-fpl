"""Tool-call log: every call through the server is recorded (who, tool, arguments, outcome,
timing) without changing the tools or failing a call when the log itself fails."""

import asyncio

import pytest

from conftest import call_tools
from mcp_fpl import call_log


@pytest.fixture
def rows(monkeypatch):
    """Capture rows as they are scheduled — no dependence on when the background write runs."""
    written = []
    monkeypatch.setattr(call_log, "enabled", lambda: True)
    monkeypatch.setattr(call_log, "_schedule", written.append)
    return written


def test_calls_are_recorded_with_client_arguments_and_outcome(app, keys, rows):
    call_tools(app, keys["friend"], [("get_my_fpl_squad", {"gameweek": 3}),
                                     ("get_fpl_league_standings", {"limit": 5})])
    assert [r["tool"] for r in rows] == ["get_my_fpl_squad", "get_fpl_league_standings"]
    squad = rows[0]
    assert squad["client_id"] == "friend" and squad["principal"] == "friend"
    assert squad["arguments"] == {"gameweek": 3}
    # the friend key has no team linked: the tool answers with an Error → tool_error
    assert squad["outcome"] == "tool_error" and "manager id" in squad["error"]
    assert isinstance(squad["duration_ms"], int) and squad["result_chars"] > 0


def test_exceptions_are_recorded_and_still_raised(rows):
    async def boom(x: int) -> dict:
        """A tool that fails."""
        raise ValueError("bad input")

    wrapped = call_log.recorded(boom, "boom")

    async def go():
        with pytest.raises(ValueError):
            await wrapped(7)

    asyncio.run(go())
    assert rows[0]["outcome"] == "exception" and "bad input" in rows[0]["error"]
    assert rows[0]["arguments"] == {"x": 7}


def test_a_failing_log_never_breaks_the_call(monkeypatch):
    monkeypatch.setattr(call_log, "enabled", lambda: True)
    attempts = []

    def broken(row):
        attempts.append(row)
        raise RuntimeError("db down")

    monkeypatch.setattr(call_log, "_write", broken)

    async def ok() -> dict:
        """Fine."""
        return {"fine": True}

    async def go():
        result = await call_log.recorded(ok, "ok")()
        await asyncio.gather(*call_log._background)  # wait for the background write itself
        return result

    assert asyncio.run(go()) == {"fine": True}
    assert len(attempts) == 1


def test_off_without_a_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert call_log.enabled() is False
    monkeypatch.setenv("MCP_CALL_LOG", "off")
    assert call_log.enabled() is False


def test_calls_are_kept_unless_a_retention_is_set(monkeypatch):
    from mcp_fpl import jobs
    from mcp_fpl.db import call_log_repo

    purged = []
    monkeypatch.setattr(call_log_repo, "purge", lambda days: purged.append(days) or 0)
    monkeypatch.delenv("MCP_CALL_LOG_DAYS", raising=False)
    jobs.purge_call_log()
    assert purged == []
    monkeypatch.setenv("MCP_CALL_LOG_DAYS", "365")
    jobs.purge_call_log()
    assert purged == [365]

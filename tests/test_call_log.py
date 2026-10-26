"""Tool-call log: every call through the server is recorded (who, tool, arguments, outcome,
timing) without changing the tools or failing a call when the log itself fails."""

import asyncio

import pytest

from conftest import call_tools
from mcp_fpl import call_log


@pytest.fixture
def rows(monkeypatch):
    written = []
    monkeypatch.setattr(call_log, "enabled", lambda: True)
    monkeypatch.setattr(call_log, "_write", written.append)
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
        await asyncio.sleep(0.05)  # let the background write run

    asyncio.run(go())
    assert rows[0]["outcome"] == "exception" and "bad input" in rows[0]["error"]
    assert rows[0]["arguments"] == {"x": 7}


def test_a_failing_log_never_breaks_the_call(monkeypatch):
    monkeypatch.setattr(call_log, "enabled", lambda: True)

    def broken(row):
        raise RuntimeError("db down")

    monkeypatch.setattr(call_log, "_write", broken)

    async def ok() -> dict:
        """Fine."""
        return {"fine": True}

    async def go():
        result = await call_log.recorded(ok, "ok")()
        await asyncio.sleep(0.05)
        return result

    assert asyncio.run(go()) == {"fine": True}


def test_off_without_a_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert call_log.enabled() is False
    monkeypatch.setenv("MCP_CALL_LOG", "off")
    assert call_log.enabled() is False

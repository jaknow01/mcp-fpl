"""Server-level checks: tool parity with the Jarvis baseline, descriptions, auth."""

import json
from pathlib import Path

from conftest import list_tools

ADMIN_TOOLS = {"list_issued_keys", "revoke_issued_key"}  # admin-only: hidden from other keys
EXTRA_TOOLS = {"get_draft_free_agents", "get_draft_activity",  # FPL Draft only
               "compare_squads"}
BASELINE = json.loads((Path(__file__).parent / "baseline.json").read_text(encoding="utf-8"))


def _props(schema: dict) -> set:
    return set((schema or {}).get("properties", {}))


def test_tool_parity_with_jarvis_baseline(app, keys):
    tools = {t.name: t for t in list_tools(app, keys["friend"])}
    expected = {t["name"]: t["input_schema"] for t in BASELINE["tools"]}
    assert set(tools) - {"whoami"} - EXTRA_TOOLS == set(expected)
    for name, schema in expected.items():
        got = tools[name].input_schema
        assert _props(got) == _props(schema), name
        assert set(got.get("required", [])) == set(schema.get("required", [])), name


def test_every_tool_has_a_description(app, keys):
    for tool in list_tools(app, keys["friend"]):
        assert tool.description and len(tool.description) > 20, tool.name


def test_admin_tools_listed_only_for_admin_keys(app, keys):
    import asyncio

    from jarvis_mcp_kit.testing import connect, serving

    async def go():
        async with serving(app):
            out = {}
            for who in ("friend", "jarvis", "admin"):
                async with connect(app, "/mcp/fpl", keys[who]) as client:
                    out[who] = {t.name for t in (await client.list_tools()).tools}
            return out

    listed = asyncio.run(go())
    assert not ADMIN_TOOLS & listed["friend"] and not ADMIN_TOOLS & listed["jarvis"]  # trusted ≠ admin
    assert ADMIN_TOOLS <= listed["admin"]

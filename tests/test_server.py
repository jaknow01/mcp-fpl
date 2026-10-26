"""Server-level checks: tool parity with the Jarvis baseline, descriptions, auth."""

import json
from pathlib import Path

from conftest import list_tools

EXTRA_TOOLS = {"list_issued_keys", "revoke_issued_key",  # admin-only, new on the server
               "get_draft_free_agents", "get_draft_activity"}  # FPL Draft only
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

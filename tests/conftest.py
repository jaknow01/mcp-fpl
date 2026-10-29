"""Shared fixtures: a clients file with a trusted key (jarvis) and a plain key (friend)."""

import asyncio

import pytest
import yaml
from jarvis_mcp_kit import new_key
from jarvis_mcp_kit.testing import clients_yaml, connect, serving, text_of

SERVER = "fpl"
PATH = f"/mcp/{SERVER}"


@pytest.fixture
def keys(tmp_path, monkeypatch):
    keys = {"jarvis": new_key(), "friend": new_key(), "admin": new_key()}
    path = tmp_path / "clients.yaml"
    path.write_text(yaml.safe_dump(clients_yaml(
        jarvis={"key": keys["jarvis"], "servers": [SERVER], "trusted": True, "rate_limit_per_minute": 1000},
        friend={"key": keys["friend"], "servers": [SERVER], "rate_limit_per_minute": 1000},
        admin={"key": keys["admin"], "servers": [SERVER], "admin": True},
    )))
    monkeypatch.setenv("MCP_CLIENTS_CONFIG", str(path))
    return keys


@pytest.fixture
def app(keys):
    from mcp_fpl.cli import create_app

    return create_app()


def call_tools(app, key, calls, on_behalf_of=None):
    """Run [(tool, args), ...] in one session; returns the CallToolResults."""

    async def go():
        async with serving(app):
            async with connect(app, PATH, key, on_behalf_of=on_behalf_of) as client:
                return [await client.call_tool(name, args) for name, args in calls]

    return asyncio.run(go())


def list_tools(app, key):
    async def go():
        async with serving(app):
            async with connect(app, PATH, key) as client:
                return (await client.list_tools()).tools

    return asyncio.run(go())


__all__ = ["call_tools", "list_tools", "text_of"]

"""Self-service keys: store, name matching, the /mcp/join flow, the issued key working over
MCP with its entry as default, and the admin list/revoke tools. Hermetic (fake roster,
JSON key store under tmp_path, fake notifier)."""

import asyncio
import re

import pytest
import yaml
from jarvis_mcp_kit import hash_key, new_key
from jarvis_mcp_kit.testing import clients_yaml, connect, http_client, serving, text_of

from mcp_fpl import keys as keys_mod
from mcp_fpl.cli import create_app
from mcp_fpl.join import LeagueRoster, match_members
from mcp_fpl.keys import JsonKeyStore, set_key_store

LEAGUE = 314
ROWS = [
    {"entry": 101, "entry_name": "Łoś Team", "player_name": "Łukasz Nowak", "rank": 1},
    {"entry": 102, "entry_name": "Kane & Able", "player_name": "Marek Kowalski", "rank": 2},
    {"entry": 103, "entry_name": "Ableton FC", "player_name": "Ola Nowak", "rank": 3},
    {"entry": 104, "entry_name": "<script>alert(1)</script>", "player_name": "Hacker", "rank": 4},
]


async def _fake_fetch(league_id, max_managers):
    assert league_id == LEAGUE
    return "Liga Znajomych", ROWS, True


def _members():
    return asyncio.run(LeagueRoster(fetch_managers=_fake_fetch, league_ids=[LEAGUE]).members())


def test_store_issue_replaces_and_revokes(tmp_path):
    store = JsonKeyStore(tmp_path / "keys.json")
    common = dict(entry_id=101, league_id=LEAGUE, manager_name="Ł", team_name="T", servers=["fpl"])
    assert store.issue(key_sha256="a" * 64, **common) == 0
    assert store.issue(key_sha256="b" * 64, **common) == 1
    assert [r["key_sha256"] for r in store.active()] == ["b" * 64]
    assert store.revoke_entry(101) == 1 and store.active() == []


@pytest.mark.parametrize("query, entries", [
    ("101", [101]), ("999", []), ("los team", [101]), ("lukasz nowak", [101]),
    ("MAREK KOWALSKI", [102]), ("nowak", [101, 103]), ("able", [102, 103]), ("", []),
])
def test_match_members(query, entries):
    assert sorted(m["entry"] for m in match_members(query, _members())) == entries


@pytest.fixture
def env(tmp_path, monkeypatch):
    admin_key = new_key()
    path = tmp_path / "clients.yaml"
    path.write_text(yaml.safe_dump(clients_yaml(admin={"key": admin_key, "servers": ["fpl"], "admin": True})))
    monkeypatch.setenv("MCP_CLIENTS_CONFIG", str(path))
    monkeypatch.setenv("FPL_LEAGUES", str(LEAGUE))
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://jarvis.example")
    monkeypatch.setenv("MCP_KEYS_REFRESH_SECONDS", "3600")
    store = JsonKeyStore(tmp_path / "keys.json")
    set_key_store(store)
    keys_mod.server.registry.invalidate()
    notices = []

    async def notify(text):
        notices.append(text)

    app = create_app(roster=LeagueRoster(fetch_managers=_fake_fetch, league_ids=[LEAGUE]), notify=notify)
    yield app, store, notices, admin_key
    set_key_store(None)


def _key_in(page: str):
    m = re.search(r"jvk_[A-Za-z0-9_-]{20,}", page)
    return m.group(0) if m else None


async def _whoami(app, key):
    async with connect(app, "/mcp/fpl", key) as c:
        return await c.call_tool("whoami", {})


def test_join_flow_issue_confirm_replace(env):
    app, store, notices, _ = env
    seen = {}

    async def flow():
        async with serving(app):
            async with http_client(app) as h:
                seen["form"] = await h.get("/join")
                seen["old_path"] = await h.get("/mcp/join")
                seen["missing"] = await h.post("/mcp/join", data={"team": "Nobody United"})
                seen["ambiguous"] = await h.post("/mcp/join", data={"team": "nowak"})
                seen["issued"] = await h.post("/mcp/join", data={"team": "Marek Kowalski"})
                key1 = _key_in(seen["issued"].text)
                seen["who1"] = await _whoami(app, key1)
                seen["again"] = await h.post("/mcp/join", data={"team": "102"})
                seen["replaced"] = await h.post("/mcp/join", data={"team": "102", "replace": "1"})
                key2 = _key_in(seen["replaced"].text)
                try:
                    await _whoami(app, key1)
                    seen["old_rejected"] = False
                except Exception:  # 401 at connect time
                    seen["old_rejected"] = True
                seen["who2"] = await _whoami(app, key2)
                await asyncio.sleep(0)
                return key1, key2

    key1, key2 = asyncio.run(flow())
    assert seen["form"].status_code == 200 and "<form" in seen["form"].text
    assert seen["old_path"].status_code == 200 and "action=''" in seen["form"].text
    assert seen["missing"].status_code == 404 and _key_in(seen["missing"].text) is None
    assert "Którą drużynę" in seen["ambiguous"].text and _key_in(seen["ambiguous"].text) is None
    assert seen["issued"].headers["cache-control"] == "no-store"
    assert f"https://jarvis.example/mcp/{key1}" in seen["issued"].text
    who1 = text_of(seen["who1"]).replace(" ", "")
    assert '"client":"fpl-102"' in who1 and '"default_entry_id":102' in who1
    assert "ma już klucz" in seen["again"].text and _key_in(seen["again"].text) is None
    assert key2 and key2 != key1
    assert seen["old_rejected"]
    assert '"client":"fpl-102"' in text_of(seen["who2"]).replace(" ", "")
    assert [r["key_sha256"] for r in store.active()] == [hash_key(key2)]
    assert len(notices) == 2 and "Zastąpił poprzedni" in notices[1]


def test_old_key_gets_401_after_replacement(env):
    app, store, _, _ = env

    async def flow():
        async with serving(app):
            async with http_client(app) as h:
                k1 = _key_in((await h.post("/mcp/join", data={"team": "101"})).text)
                await h.post("/mcp/join", data={"team": "101", "replace": "1"})
                body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                return (await h.post(f"/mcp/fpl/{k1}", json=body,
                                     headers={"Accept": "application/json, text/event-stream"})).status_code

    assert asyncio.run(flow()) == 401


def test_join_escapes_team_names(env):
    app, *_ = env

    async def flow():
        async with serving(app):
            async with http_client(app) as h:
                return await h.post("/mcp/join", data={"team": "104"})

    page = asyncio.run(flow()).text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_join_rate_limit_per_ip(env, monkeypatch):
    app, *_ = env
    monkeypatch.setenv("MCP_JOIN_PER_HOUR", "2")

    async def flow():
        async with serving(app):
            async with http_client(app) as h:
                hdr = {"X-Forwarded-For": "203.0.113.9"}
                codes = [(await h.post("/mcp/join", data={"team": "x"}, headers=hdr)).status_code for _ in range(3)]
                other = await h.post("/mcp/join", data={"team": "x"}, headers={"X-Forwarded-For": "198.51.100.1"})
                return codes, other.status_code

    codes, other = asyncio.run(flow())
    assert codes == [404, 404, 429] and other == 404


def test_admin_tools(env):
    app, store, _, admin_key = env
    store.issue(key_sha256="c" * 64, entry_id=103, league_id=LEAGUE, manager_name="Ola Nowak",
                team_name="Ableton FC", servers=["fpl"])
    friend_key = new_key()
    store.issue(key_sha256=hash_key(friend_key), entry_id=101, league_id=LEAGUE, manager_name="Ł",
                team_name="Łoś Team", servers=["fpl"])
    keys_mod.server.registry.invalidate()

    async def flow():
        async with serving(app):
            async with connect(app, "/mcp/fpl", friend_key) as c:
                denied = await c.call_tool("revoke_issued_key", {"entry_id": 103})
            async with connect(app, "/mcp/fpl", admin_key) as c:
                listing = await c.call_tool("list_issued_keys", {})
                revoked = await c.call_tool("revoke_issued_key", {"entry_id": 103})
            return denied, listing, revoked

    denied, listing, revoked = asyncio.run(flow())
    assert denied.is_error and "admin" in text_of(denied)
    assert "Ableton FC" in text_of(listing) and "c" * 64 not in text_of(listing)
    assert '"revoked": 1' in text_of(revoked)
    assert [r["entry_id"] for r in store.active()] == [101]

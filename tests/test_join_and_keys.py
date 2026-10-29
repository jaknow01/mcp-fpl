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
    {"entry": 105, "entry_name": "Dwa Razy", "player_name": "Jan A", "rank": 5},
    {"entry": 106, "entry_name": "dwa  razy", "player_name": "Jan B", "rank": 6},
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
    ("101", [101]), ("999", []), ("ŁOŚ TEAM", [101]), ("  łoś   team ", [101]),
    ("los team", []),                      # Polish letters must match
    ("Łoś", []), ("able", []),             # no partial matches
    ("Łukasz Nowak", []),                  # manager names are not team names
    ("dwa razy", [105, 106]), ("", []),    # an exact name shared by two teams
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


def test_join_flow_exact_match_and_one_key_per_team(env):
    app, store, notices, admin_key = env
    seen = {}

    async def flow():
        async with serving(app):
            async with http_client(app) as h:
                seen["form"] = await h.get("/join")
                seen["old_path"] = await h.get("/mcp/join")
                seen["missing"] = await h.post("/mcp/join", data={"team": "Nobody United"})
                seen["partial"] = await h.post("/mcp/join", data={"team": "Kane"})
                seen["manager"] = await h.post("/mcp/join", data={"team": "Marek Kowalski"})
                seen["twins"] = await h.post("/mcp/join", data={"team": "Dwa razy"})
                seen["issued"] = await h.post("/mcp/join", data={"team": "kane & able"})
                key1 = _key_in(seen["issued"].text)
                seen["who1"] = await _whoami(app, key1)
                seen["again_id"] = await h.post("/mcp/join", data={"team": "102"})
                seen["again_name"] = await h.post("/mcp/join", data={"team": "Kane & Able", "replace": "1"})
                async with connect(app, "/mcp/fpl", admin_key) as c:  # a lost key: the admin revokes it
                    await c.call_tool("revoke_issued_key", {"entry_id": 102})
                seen["after_revoke"] = await h.post("/mcp/join", data={"team": "102"})
                await asyncio.sleep(0)
                return key1

    key1 = asyncio.run(flow())
    assert seen["form"].status_code == 200 and "<form" in seen["form"].text
    assert seen["old_path"].status_code == 200 and "action=''" in seen["form"].text
    refused = "nie ma w dopuszczonych ligach albo ma już wygenerowany klucz"
    for name in ("missing", "partial", "manager", "again_id", "again_name"):
        page = seen[name]
        assert page.status_code == 404 and refused in page.text and _key_in(page.text) is None, name
        assert "Którą drużynę" not in page.text, name
    # the same answer for "unknown" and "already has a key": nothing to tell them apart
    assert seen["missing"].text.replace("Nobody United", "X") == seen["again_id"].text.replace("102", "X")
    assert seen["twins"].status_code == 409 and "wpisz ID" in seen["twins"].text
    assert _key_in(seen["twins"].text) is None and "Jan A" not in seen["twins"].text
    assert seen["issued"].headers["cache-control"] == "no-store"
    assert f"https://jarvis.example/mcp/{key1}" in seen["issued"].text
    assert "Claude" not in seen["form"].text and "agenta" in seen["form"].text
    tabs = re.findall(r"data-tab='p-(\w+)'[^>]*aria-selected='(\w+)'", seen["issued"].text)
    assert [t for t, _ in tabs] == ["claude", "chatgpt", "openai", "cursor", "vscode", "gemini", "local", "other"]
    assert [t for t, sel in tabs if sel == "true"] == ["claude"]
    assert f"Bearer {key1}" in seen["issued"].text
    assert "No sign-in" in seen["issued"].text
    who1 = text_of(seen["who1"]).replace(" ", "")
    assert '"client":"fpl-102"' in who1 and '"default_entry_id":102' in who1
    key2 = _key_in(seen["after_revoke"].text)
    assert key2 and key2 != key1
    assert [r["key_sha256"] for r in store.active()] == [hash_key(key2)]
    assert len(notices) == 2


def test_revoked_key_gets_401(env):
    app, store, _, admin_key = env

    async def flow():
        async with serving(app):
            async with http_client(app) as h:
                k1 = _key_in((await h.post("/mcp/join", data={"team": "101"})).text)
                async with connect(app, "/mcp/fpl", admin_key) as c:
                    await c.call_tool("revoke_issued_key", {"entry_id": 101})
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
    assert denied.is_error and text_of(denied) == "Unknown tool: revoke_issued_key"
    assert "Ableton FC" in text_of(listing) and "c" * 64 not in text_of(listing)
    assert '"revoked": 1' in text_of(revoked)
    assert [r["entry_id"] for r in store.active()] == [101]


# ── FPL Draft on /join ───────────────────────────────────────────────────────

DRAFT_LEAGUE = 1000
DRAFT_ROWS = [  # draft ids are their own id space: 101 here is NOT classic 101
    {"entry": 101, "entry_name": "Draftowi Mistrzowie", "player_name": "Zosia Draft"},
    {"entry": 2489, "entry_name": "Kurek XI", "player_name": "Hamit Kurek"},
]


async def _fake_draft_fetch(league_id, max_managers):
    assert league_id == DRAFT_LEAGUE
    return "Draft Mates", DRAFT_ROWS, True


@pytest.fixture
def env_draft(env, monkeypatch):
    _app, store, notices, admin_key = env
    app = create_app(roster=LeagueRoster(fetch_managers=_fake_fetch, league_ids=[LEAGUE]),
                     draft_roster=LeagueRoster(fetch_managers=_fake_draft_fetch, league_ids=[DRAFT_LEAGUE]),
                     notify=lambda text: _record(notices, text))
    return app, store, notices, admin_key


async def _record(notices, text):
    notices.append(text)


def _post_all(app, *forms, whoami=False):
    """POST the forms in order; with ``whoami`` also call whoami with the last page's key."""
    async def flow():
        async with serving(app):
            async with http_client(app) as h:
                pages = [await h.post("/join", data=f) for f in forms]
                await asyncio.sleep(0)
                if whoami:
                    pages.append(await _whoami(app, _key_in(pages[-1].text)))
                return pages
    return asyncio.run(flow())


def test_form_shows_draft_field_only_when_draft_leagues_configured(env, env_draft):
    async def get(app):
        async with serving(app):
            async with http_client(app) as h:
                return (await h.get("/join")).text
    assert "name='draft'" not in asyncio.run(get(env[0]))
    page = asyncio.run(get(env_draft[0]))
    assert "name='team'" in page and "name='draft'" in page and "oba pola" in page


def test_draft_only_key(env_draft):
    app, store, notices, _ = env_draft
    empty, page, who = _post_all(app, {}, {"draft": "Kurek XI"}, whoami=True)
    assert empty.status_code == 400 and _key_in(empty.text) is None
    assert _key_in(page.text) and "FPL Draft" in page.text and "waiverów" in page.text
    who = text_of(who).replace(" ", "")
    assert '"client":"fpl-d2489"' in who and '"default_draft_entry_id":2489' in who
    assert '"default_entry_id":null' in who
    row = store.active()[0]
    assert row["entry_id"] is None and (row["draft_entry_id"], row["draft_league_id"]) == (2489, DRAFT_LEAGUE)
    assert "--draft" in notices[0]


def test_both_games_one_key_and_separate_id_spaces(env_draft):
    app, store, _, _ = env_draft
    # 101 is "Łoś Team" in classic and "Draftowi Mistrzowie" in draft — each field searches its own game
    page, who = _post_all(app, {"team": "101", "draft": "101"}, whoami=True)
    assert "Łoś Team" in page.text and "Draftowi Mistrzowie" in page.text
    who = text_of(who).replace(" ", "")
    assert '"client":"fpl-101"' in who and '"default_entry_id":101' in who
    assert '"default_draft_entry_id":101' in who and f'"default_draft_league_id":{DRAFT_LEAGUE}' in who
    assert len(store.active()) == 1


def test_draft_name_is_exact_and_case_insensitive(env_draft):
    app, *_ = env_draft
    partial, exact = _post_all(app, {"draft": "Kurek"}, {"draft": "kurek xi"})
    assert partial.status_code == 404 and "FPL Draft" in partial.text and _key_in(partial.text) is None
    assert _key_in(exact.text) and "Kurek XI" in exact.text


def test_a_taken_team_blocks_the_whole_request(env_draft):
    app, store, notices, _ = env_draft
    first, second = _post_all(app, {"team": "102", "draft": "2489"}, {"team": "103", "draft": "Kurek XI"})
    assert _key_in(first.text)
    # classic 103 is free, but the draft team already has a key: no key at all
    assert second.status_code == 404 and _key_in(second.text) is None
    assert "nie ma w dopuszczonych ligach albo ma już wygenerowany klucz" in second.text
    assert "id='team' name='team' type='text' value='103'" in second.text  # the form keeps the input
    (row,) = store.active()
    assert (row["entry_id"], row["draft_entry_id"]) == (102, 2489) and len(notices) == 1


def test_store_revokes_by_draft_entry(tmp_path):
    store = JsonKeyStore(tmp_path / "keys.json")
    store.issue(key_sha256="a" * 64, draft_entry_id=7, draft_league_id=1, manager_name="Z", servers=["fpl"])
    store.issue(key_sha256="b" * 64, entry_id=7, league_id=2, manager_name="K", servers=["fpl"])
    assert store.revoke_entry(7, "draft") == 1
    assert [r["key_sha256"] for r in store.active()] == ["b" * 64]  # classic 7 untouched
    with pytest.raises(ValueError):
        store.issue(key_sha256="c" * 64, manager_name="?", servers=["fpl"])

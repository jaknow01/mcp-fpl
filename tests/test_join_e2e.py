"""End-to-end /join for every real member of the configured leagues, against the live FPL API.

Each person does what a friend would do: open /join, type their team (both teams when they
play both games — paired by manager name), get a key, then use that key over MCP: whoami,
their squad, the league table, compare_squads. A second attempt for the same team must be
refused. Runs a local app with a throwaway JSON key store, so no real key is issued.

Opt-in (network, ~1 min): FPL_E2E=1 uv run pytest tests/test_join_e2e.py -v
Leagues come from FPL_LEAGUES / FPL_DRAFT_LEAGUES (the environment, else .env).
"""

import asyncio
import json
import os
import re
from pathlib import Path

import pytest
import yaml
from jarvis_mcp_kit import hash_key, new_key
from jarvis_mcp_kit.testing import clients_yaml, connect, http_client, serving, text_of

pytestmark = pytest.mark.skipif(os.getenv("FPL_E2E") != "1", reason="live e2e: set FPL_E2E=1")

REFUSED = "nie ma w dopuszczonych ligach albo ma już wygenerowany klucz"
GAME_FIELD = {"classic": "team", "draft": "draft"}


def _env_leagues() -> dict:
    values = {k: os.getenv(k) for k in ("FPL_LEAGUES", "FPL_DRAFT_LEAGUES")}
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() in values and not values[k.strip()]:
                values[k.strip()] = v.strip().strip("'\"")
    return {k: v for k, v in values.items() if v}


def _people() -> list[dict]:
    """Everyone in the configured leagues, one person per manager name, with their teams."""
    from mcp_fpl.draft_api import list_draft_league_managers
    from mcp_fpl.fpl_api import list_league_managers

    leagues = _env_leagues()
    ids = lambda key: [int(x) for x in leagues.get(key, "").split(",") if x.strip()]  # noqa: E731

    async def load():
        people: dict[str, dict] = {}
        for game, key, fetch in (("classic", "FPL_LEAGUES", list_league_managers),
                                 ("draft", "FPL_DRAFT_LEAGUES", list_draft_league_managers)):
            for lid in ids(key):
                _, rows, _ = await fetch(lid, 500)
                for r in rows:
                    person = people.setdefault(" ".join(r["player_name"].casefold().split()),
                                               {"manager": r["player_name"], "teams": {}})
                    person["teams"][game] = {"entry": r["entry"], "name": r["entry_name"], "league": lid}
        return sorted(people.values(), key=lambda p: p["manager"].casefold())

    return asyncio.run(load()) if os.getenv("FPL_E2E") == "1" else []


PEOPLE = _people()


def _typed(team: dict, i: int) -> str:
    """How the person types their team: the id, the exact name, or the name in other case
    with stray spaces — all accepted by /join."""
    style = i % 3
    if style == 0:
        return str(team["entry"])
    if style == 1:
        return team["name"]
    return "  " + "  ".join(team["name"].swapcase().split()) + " "


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    from mcp_fpl import keys as keys_mod
    from mcp_fpl.cli import create_app
    from mcp_fpl.keys import JsonKeyStore, set_key_store

    tmp = tmp_path_factory.mktemp("e2e")
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("DATABASE_URL", raising=False)  # no Postgres: JSON keys, no call log
        for k, v in _env_leagues().items():
            mp.setenv(k, v)
        clients = tmp / "clients.yaml"
        clients.write_text(yaml.safe_dump(clients_yaml(admin={"key": new_key(), "servers": ["fpl"], "admin": True})))
        mp.setenv("MCP_CLIENTS_CONFIG", str(clients))
        mp.setenv("MCP_PUBLIC_URL", "https://fpl.example")
        mp.setenv("MCP_JOIN_PER_HOUR", "1000")
        mp.setenv("MCP_KEYS_REFRESH_SECONDS", "3600")
        store = JsonKeyStore(tmp / "keys.json")
        set_key_store(store)
        keys_mod.server.registry.invalidate()
        notices = []

        async def notify(text):
            notices.append(text)

        yield {"app": create_app(notify=notify), "store": store, "notices": notices}
        set_key_store(None)


def _key_in(page: str):
    m = re.search(r"jvk_[A-Za-z0-9_-]{20,}", page)
    return m.group(0) if m else None


def _json(result) -> dict:
    return json.loads(text_of(result))


def test_found_people():
    assert PEOPLE, "no league members found — check FPL_LEAGUES / FPL_DRAFT_LEAGUES"


@pytest.fixture(scope="module")
def runs(live):
    """Every person's visit, one after another, against one running app (an app can be
    served only once): {manager: (key, seen)} or {manager: the exception}."""
    app, out = live["app"], {}

    async def visit(h, i, person):
        teams = person["teams"]
        form = {GAME_FIELD[g]: _typed(t, i) for g, t in teams.items()}
        seen = {"page": await h.get("/join"), "issued": await h.post("/join", data=form)}
        key = _key_in(seen["issued"].text)
        assert key, f"no key for {form}: {seen['issued'].status_code}"
        async with connect(app, "/mcp/fpl", key) as c:
            seen["whoami"] = _json(await c.call_tool("whoami", {}))
            for game in teams:
                seen[f"squad_{game}"] = _json(await c.call_tool("get_my_fpl_squad", {"game": game}))
                seen[f"table_{game}"] = _json(await c.call_tool("get_fpl_league_standings", {"game": game}))
                seen[f"compare_{game}"] = _json(await c.call_tool("compare_squads", {"game": game}))
            seen["no_game"] = _json(await c.call_tool("get_my_fpl_squad", {}))
        seen["again"] = await h.post("/join", data=form)
        return key, seen

    async def all_visits():
        async with serving(app):
            async with http_client(app) as h:
                for i, person in enumerate(PEOPLE):
                    try:
                        out[person["manager"]] = await visit(h, i, person)
                    except Exception as e:  # reported by that person's test
                        out[person["manager"]] = e
            await asyncio.sleep(0)

    asyncio.run(all_visits())
    return out


@pytest.mark.parametrize("person", PEOPLE, ids=[p["manager"] for p in PEOPLE])
def test_friend_gets_a_working_key(live, runs, person):
    teams, result = person["teams"], runs[person["manager"]]
    if isinstance(result, Exception):
        raise result
    key, seen = result

    page = seen["page"].text
    assert seen["page"].status_code == 200 and "name='team'" in page and "name='draft'" in page
    assert seen["issued"].status_code == 200 and "Gotowe" in seen["issued"].text
    for t in teams.values():
        assert t["name"] in seen["issued"].text  # the page names the team(s) the key is for

    settings = seen["whoami"]
    classic, draft = teams.get("classic"), teams.get("draft")
    assert settings.get("default_entry_id") == (classic and classic["entry"])
    assert settings.get("default_league_id") == (classic and classic["league"])
    if draft:
        assert settings["default_draft_entry_id"] == draft["entry"]
        assert settings["default_draft_league_id"] == draft["league"]

    for game, team in teams.items():
        squad = seen[f"squad_{game}"]
        assert "Error" not in squad, squad
        assert len(squad["squad"]) == 15
        table = seen[f"table_{game}"]
        mine = [r for r in table["standings"] if r.get("is_me")]
        assert [r["team_name"] for r in mine] == [team["name"]], table
        compared = seen[f"compare_{game}"]
        assert "Error" not in compared and compared["rivals"], compared
        assert compared["me"]["team_name"] == team["name"]

    if len(teams) == 2:  # both games on one key: without game= the agent is asked to pick
        assert "both" in seen["no_game"]["Error"]
    else:
        assert "Error" not in seen["no_game"] and len(seen["no_game"]["squad"]) == 15

    again = seen["again"]
    assert again.status_code == 404 and REFUSED in again.text and _key_in(again.text) is None
    assert hash_key(key) in {r["key_sha256"] for r in live["store"].active()}


def test_every_team_has_exactly_one_key(live, runs):
    active = live["store"].active()
    classic = sorted(r["entry_id"] for r in active if r.get("entry_id") is not None)
    draft = sorted(r["draft_entry_id"] for r in active if r.get("draft_entry_id") is not None)
    assert len(active) == len(PEOPLE)
    assert classic == sorted(p["teams"]["classic"]["entry"] for p in PEOPLE if "classic" in p["teams"])
    assert draft == sorted(p["teams"]["draft"]["entry"] for p in PEOPLE if "draft" in p["teams"])
    assert len(live["notices"]) == len(PEOPLE)

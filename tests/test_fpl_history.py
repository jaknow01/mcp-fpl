"""Tests for the league-history read tool shaping (no network, no DB — the underlying
load_league_history is monkeypatched). The caller's key links league 999 as its default."""
import asyncio

import pytest
from jarvis_mcp_kit import Client, acting_as

from mcp_fpl import tools as ft


@pytest.fixture
def caller_in_league_999():
    client = Client("kuba", "Kuba", "0" * 64, frozenset({"fpl"}), settings={"fpl": {"league_id": 999}})
    with acting_as(client, None):
        yield client


def _snap(day, gw, managers):
    return {"date": day, "gameweek": gw, "data": {"league_name": "Liga", "managers": managers}}


def _fake_history():
    first = [
        {"entry": 1, "manager": "Kuba", "team": "Wombat FC", "rank": 2, "last_rank": 2, "total": 100, "event_total": 40,
         "picks": [{"element": 10, "name": "Haaland", "position": "FWD", "pick_position": 1, "is_captain": True}]},
        {"entry": 2, "manager": "Ala", "team": "Ala United", "rank": 1, "last_rank": 1, "total": 120, "event_total": 30,
         "picks": [{"element": 11, "name": "Salah", "position": "MID", "pick_position": 1, "is_captain": True}]},
    ]
    last = [
        {"entry": 1, "manager": "Kuba", "team": "Wombat FC", "rank": 1, "last_rank": 2, "total": 180, "event_total": 80,
         "picks": [{"element": 12, "name": "Palmer", "position": "MID", "pick_position": 1, "is_captain": True}]},
        {"entry": 2, "manager": "Ala", "team": "Ala United", "rank": 2, "last_rank": 1, "total": 160, "event_total": 40,
         "picks": [{"element": 11, "name": "Salah", "position": "MID", "pick_position": 1, "is_captain": False}]},
    ]
    return [_snap("2026-01-01", 20, first), _snap("2026-01-15", 21, last)]


def test_manager_matches():
    row = {"manager": "Kuba Nowak", "team": "Wombat FC"}
    assert ft._manager_matches(row, "kuba")
    assert ft._manager_matches(row, "wombat")
    assert not ft._manager_matches(row, "ala")
    assert ft._manager_matches(row, "")  # empty matches all


def test_standings_movement(monkeypatch, caller_in_league_999):
    monkeypatch.setattr(ft, "load_league_history", lambda *a, **k: _fake_history())
    out = asyncio.run(ft.get_league_history(view="standings", days=30))
    moves = {m["manager"]: m for m in out["standings_movement"]}
    assert moves["Kuba"]["rank_delta"] == 1        # 2 -> 1 = climbed one
    assert moves["Kuba"]["points_gained"] == 80    # 180 - 100
    assert out["standings_movement"][0]["manager"] == "Kuba"  # sorted by current rank (1st)


def test_standings_manager_series(monkeypatch, caller_in_league_999):
    monkeypatch.setattr(ft, "load_league_history", lambda *a, **k: _fake_history())
    out = asyncio.run(ft.get_league_history(view="standings", days=30, manager="Kuba"))
    assert [p["rank"] for p in out["series"]] == [2, 1]
    assert [p["total"] for p in out["series"]] == [100, 180]


def test_squads_by_manager(monkeypatch, caller_in_league_999):
    monkeypatch.setattr(ft, "load_league_history", lambda *a, **k: _fake_history())
    out = asyncio.run(ft.get_league_history(view="squads", days=30, manager="Kuba"))
    gws = {s["gameweek"] for s in out["squads_by_gameweek"]}
    assert gws == {20, 21}
    names = {p["name"] for s in out["squads_by_gameweek"] for p in s["squad"]}
    assert names == {"Haaland", "Palmer"}


def test_no_history(monkeypatch, caller_in_league_999):
    monkeypatch.setattr(ft, "load_league_history", lambda *a, **k: [])
    out = asyncio.run(ft.get_league_history(view="standings", days=30))
    assert "message" in out


def test_no_league_linked_asks_for_league_id(monkeypatch):
    monkeypatch.setenv("FPL_LEAGUE_ID", "999")  # a server-wide env must NOT be used
    with acting_as(Client("friend", "F", "1" * 64, frozenset({"fpl"})), None):
        out = asyncio.run(ft.get_league_history(view="standings", days=30))
    assert "league" in out["Tip"].lower() and "Error" in out

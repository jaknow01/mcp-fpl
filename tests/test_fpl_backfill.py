"""Test the per-gameweek FPL history backfill reconstruction (no network, no DB — the FPL
API fetchers, league listing and snapshot writer are monkeypatched)."""
import asyncio

import mcp_fpl.db.connection as dbconn
import mcp_fpl.db.history_repo as hist_repo
from mcp_fpl import fpl_api


BOOTSTRAP = {
    "events": [
        {"id": 1, "deadline_time": "2026-08-21T10:00:00Z"},
        {"id": 2, "is_current": True, "deadline_time": "2026-08-28T10:00:00Z"},
    ],
    "teams": [{"id": 1, "name": "Team A", "short_name": "A"}],
    "elements": [
        {"id": 10, "web_name": "Haaland", "team": 1, "element_type": 4},
        {"id": 11, "web_name": "Salah", "team": 1, "element_type": 3},
    ],
    "element_types": [
        {"id": 3, "singular_name_short": "MID"},
        {"id": 4, "singular_name_short": "FWD"},
    ],
}

HISTORIES = {
    1: [  # Kuba
        {"event": 1, "points": 50, "total_points": 50, "overall_rank": 100},
        {"event": 2, "points": 60, "total_points": 110, "overall_rank": 80},
    ],
    2: [  # Ala
        {"event": 1, "points": 70, "total_points": 70, "overall_rank": 50},
        {"event": 2, "points": 30, "total_points": 100, "overall_rank": 60},
    ],
}


def _setup(monkeypatch):
    async def fake_bootstrap(force=False):
        return BOOTSTRAP

    async def fake_list_managers(league_id, max_managers):
        rows = [
            {"entry": 1, "player_name": "Kuba", "entry_name": "Wombat FC"},
            {"entry": 2, "player_name": "Ala", "entry_name": "Ala United"},
        ]
        return "Liga Testowa", rows, True

    async def fake_fetch(path):
        if path.startswith("entry/1/history"):
            return {"current": HISTORIES[1]}
        if path.startswith("entry/2/history"):
            return {"current": HISTORIES[2]}
        if "/event/" in path and path.endswith("picks/"):
            return {"picks": [{"element": 10, "position": 1, "multiplier": 2, "is_captain": True}]}
        raise AssertionError(f"unexpected path {path}")

    saved = []
    monkeypatch.setattr(fpl_api, "fetch_fpl_bootstrap", fake_bootstrap)
    monkeypatch.setattr(fpl_api, "list_league_managers", fake_list_managers)
    monkeypatch.setattr(fpl_api, "fetch_fpl", fake_fetch)
    monkeypatch.setattr(dbconn, "is_configured", lambda: True)
    monkeypatch.setattr(hist_repo, "save_snapshot",
                        lambda lid, d, gw, name, data: saved.append((lid, str(d), gw, data)))
    return saved


def test_backfill_reconstructs_per_gameweek_ranks(monkeypatch):
    saved = _setup(monkeypatch)
    result = asyncio.run(fpl_api.backfill_league_history(11289, from_gw=1))

    assert result["league_name"] == "Liga Testowa"
    assert [gw["gameweek"] for gw in result["gameweeks"]] == [1, 2]
    assert [gw["date"] for gw in result["gameweeks"]] == ["2026-08-21", "2026-08-28"]

    by_gw = {gw: data for (_lid, _d, gw, data) in saved}

    # GW1: Ala (70) leads Kuba (50).
    gw1 = {m["manager"]: m for m in by_gw[1]["managers"]}
    assert gw1["Ala"]["rank"] == 1 and gw1["Ala"]["total"] == 70
    assert gw1["Kuba"]["rank"] == 2 and gw1["Kuba"]["event_total"] == 50

    # GW2: Kuba (110) overtakes Ala (100) — rank reconstruction must flip.
    gw2 = {m["manager"]: m for m in by_gw[2]["managers"]}
    assert gw2["Kuba"]["rank"] == 1 and gw2["Kuba"]["total"] == 110
    assert gw2["Ala"]["rank"] == 2 and gw2["Ala"]["total"] == 100

    # Squads are name-resolved.
    assert gw2["Kuba"]["picks"][0]["name"] == "Haaland"
    assert gw2["Kuba"]["picks"][0]["is_captain"] is True


def test_backfill_requires_db(monkeypatch):
    monkeypatch.setattr(dbconn, "is_configured", lambda: False)
    result = asyncio.run(fpl_api.backfill_league_history(11289))
    assert "error" in result

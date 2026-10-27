"""compare_squads and the multi-player who_owns_player_in_league (classic + Draft).
Hermetic: the FPL calls are monkeypatched with small fixtures."""

import pytest
from jarvis_mcp_kit import acting_as

from mcp_fpl import tools as ft
from test_draft import CLASSIC_BOOTSTRAP, PICKS, _caller, draft_api_fake, draft_caller, run  # noqa: F401

# classic ids: 1 Raya, 2 Saliba, 3 Haaland, 4 Gvardiol, 5 Timber
ROWS = {
    7: {"entry": 7, "entry_name": "My XI", "player_name": "Me Myself", "rank": 2, "total": 500},
    8: {"entry": 8, "entry_name": "Leaders FC", "player_name": "Ann Top", "rank": 1, "total": 520},
    9: {"entry": 9, "entry_name": "Bottom Utd", "player_name": "Bob Low", "rank": 3, "total": 400},
    10: {"entry": 10, "entry_name": "Ghost", "player_name": "No Squad", "rank": 4, "total": 300},
}


def _picks(*elements, captain=None):
    return [{"element": el, "position": i + 1, "is_captain": el == captain}
            for i, el in enumerate(elements)]


PAIRS = [
    (ROWS[8], _picks(1, 3, 4, captain=3)),
    (ROWS[7], _picks(1, 2, 3, captain=3)),
    (ROWS[9], _picks(2, 5, captain=5)),
    (ROWS[10], None),
]


@pytest.fixture
def classic_fake(monkeypatch):
    scans = []

    async def boot(force=False):
        return CLASSIC_BOOTSTRAP

    async def squads(lid, gw, max_managers=50, force=False):
        scans.append((lid, gw))
        return "Mates", PAIRS

    async def fetch_fpl(path):
        assert path == "entry/7/event/8/picks/", path
        return {"picks": _picks(1, 2, 3, captain=3)}

    monkeypatch.setattr(ft, "fetch_fpl_bootstrap", boot)
    monkeypatch.setattr(ft, "load_league_squads", squads)
    monkeypatch.setattr(ft, "fetch_fpl", fetch_fpl)
    with acting_as(_caller(entry_id=7, league_id=55), None):
        yield scans


# ── compare_squads, classic ──────────────────────────────────────────────────

def test_compare_classic_with_the_whole_league(classic_fake):
    out = run(ft.compare_squads())
    assert out["game"] == "classic" and out["gameweek"] == 8 and out["league_name"] == "Mates"
    assert out["me"] == {"team_name": "My XI", "rank": 2, "captain": "Haaland (MCI, FWD)",
                         "squad": ["Raya (ARS, GKP)", "Saliba (ARS, DEF)", "Haaland (MCI, FWD)"]}
    leader, bottom = out["rivals"]
    assert leader["team_name"] == "Leaders FC" and leader["shared_count"] == 2
    assert leader["shared"] == ["Raya (ARS, GKP)", "Haaland (MCI, FWD)"]
    assert leader["only_mine"] == ["Saliba (ARS, DEF)"]
    assert leader["only_theirs"] == ["Gvardiol (MCI, DEF)"]
    assert bottom["their_captain"] == "Timber (ARS, DEF)"
    owners = {p["player"]: p["owners"] for p in out["my_players"]}
    assert owners["Haaland (MCI, FWD)"] == ["Leaders FC"]
    assert owners["Saliba (ARS, DEF)"] == ["Bottom Utd"]
    assert out["unavailable_squads"] == ["Ghost"]
    assert classic_fake == [(55, 8)]  # one league scan for the whole comparison


def test_compare_classic_with_named_rivals(classic_fake):
    out = run(ft.compare_squads(managers=["bob", "Nobody"]))
    assert [r["team_name"] for r in out["rivals"]] == ["Bottom Utd"]
    assert out["unmatched_managers"] == ["Nobody"]


def test_compare_classic_when_i_am_outside_the_scan(classic_fake, monkeypatch):
    async def squads(lid, gw, max_managers=50, force=False):
        return "Mates", [p for p in PAIRS if p[0]["entry"] != 7]

    monkeypatch.setattr(ft, "load_league_squads", squads)
    out = run(ft.compare_squads())
    assert out["me"]["squad"][0] == "Raya (ARS, GKP)" and out["me"]["team_name"] is None


def test_compare_needs_my_team():
    with acting_as(_caller(league_id=55), None):
        assert "No FPL manager id" in run(ft.compare_squads())["Error"]


# ── who_owns_player_in_league with a list, classic ──────────────────────────

def test_who_owns_single_player_keeps_its_shape(classic_fake):
    out = run(ft.who_owns_player_in_league("Haaland"))
    assert out["player"] == "Haaland" and out["owner_count"] == 2 and out["managers_scanned"] == 3
    assert [o["team_name"] for o in out["owners"]] == ["Leaders FC", "My XI"]
    assert "players" not in out


def test_who_owns_several_players_in_one_call(classic_fake):
    out = run(ft.who_owns_player_in_league(["Haaland", "Timber", "Nobodyson", "Haaland"]))
    assert classic_fake == [(55, 8)]
    assert [b.get("player") for b in out["players"]] == ["Haaland", "Timber", None]
    assert out["players"][1]["owners"][0]["is_captain"]
    assert out["players"][2] == {"query": "Nobodyson", "Error": "No FPL player matched 'Nobodyson'."}


def test_who_owns_unknown_single_player_skips_the_scan(classic_fake):
    out = run(ft.who_owns_player_in_league("Nobodyson"))
    assert out == {"Error": "No FPL player matched 'Nobodyson'."} and classic_fake == []


# ── Draft ────────────────────────────────────────────────────────────────────

def test_draft_who_owns_several(draft_api_fake, draft_caller):
    out = run(ft.who_owns_player_in_league(["Haaland", "Saliba"]))
    assert out["game"] == "draft" and out["league_name"] == "Draft Mates"
    haaland, saliba = out["players"]
    assert haaland["owner"]["team_name"] == "UpTheBoro" and not saliba["owned"]
    assert "note_on_unowned" in out


def test_draft_compare(draft_api_fake, draft_caller, monkeypatch):
    real = ft.fetch_draft

    async def fetch_draft(path):
        if path == "entry/38724/event/8":
            return {"picks": [{"element": 4, "position": 1}, {"element": 5, "position": 12}]}
        return await real(path)

    monkeypatch.setattr(ft, "fetch_draft", fetch_draft)
    out = run(ft.compare_squads())
    assert out["game"] == "draft" and out["gameweek"] == 8
    assert out["me"]["team_name"] == "Kurek XI" and out["me"]["rank"] == 2
    assert out["me"]["starting_xi"] == ["Raya (ARS, GKP)", "Haaland (MCI, FWD)"]
    assert out["me"]["bench"] == ["NewSigning (MCI, MID)"]
    rival = out["rivals"][0]
    assert rival["team_name"] == "UpTheBoro" and rival["rank"] == 1
    assert rival["season_points_total"] == 75 and rival["form_total"] == 13.0


def test_draft_compare_unmatched_rival(draft_api_fake, draft_caller):
    out = run(ft.compare_squads(managers=["nobody"]))
    assert out["rivals"] == [] and out["unmatched_managers"] == ["nobody"]
    assert out["me"]["team_name"] == "Kurek XI"

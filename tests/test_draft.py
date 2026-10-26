"""FPL Draft support: game resolution, id translations and the draft paths of the tools.
Hermetic — the draft/classic API calls are monkeypatched with small fixtures shaped like
the real responses (checked against draft.premierleague.com/api)."""

import asyncio

import pytest
from jarvis_mcp_kit import Client, acting_as

from mcp_fpl import draft_api, identity, tools as ft

# ── fixtures shaped like the draft API ───────────────────────────────────────

TEAMS = [{"id": 1, "code": 3, "name": "Arsenal", "short_name": "ARS"},
         {"id": 2, "code": 43, "name": "Man City", "short_name": "MCI"}]
TYPES = [{"id": 1, "singular_name_short": "GKP"}, {"id": 2, "singular_name_short": "DEF"},
         {"id": 3, "singular_name_short": "MID"}, {"id": 4, "singular_name_short": "FWD"}]


def _el(id_, code, name, team, pos, form="0.0", points=0, status="a", ep="0.0"):
    return {"id": id_, "code": code, "web_name": name, "first_name": name, "second_name": "",
            "team": team, "element_type": pos, "form": form, "total_points": points,
            "points_per_game": "0.0", "ep_next": ep, "status": status, "news": ""}


DRAFT_BOOTSTRAP = {"teams": TEAMS, "element_types": TYPES, "elements": [
    _el(1, 100, "Raya", 1, 1, form="3.0", points=40),
    _el(2, 200, "Saliba", 1, 2, form="5.0", points=50),
    _el(3, 300, "Haaland", 2, 4, form="9.0", points=90),
    _el(4, 400, "Gvardiol", 2, 2, form="6.0", points=45),
    _el(5, 500, "Timber", 1, 2, form="7.0", points=30, status="i"),
    _el(600, 777, "NewSigning", 2, 3, form="4.0", points=12),   # draft id differs from classic
]}
# classic: same players, but the mid-season signing has classic id 650
CLASSIC_BOOTSTRAP = {"teams": TEAMS, "element_types": TYPES, "elements": [
    {**_el(1, 100, "Raya", 1, 1)}, {**_el(2, 200, "Saliba", 1, 2)}, {**_el(3, 300, "Haaland", 2, 4)},
    {**_el(4, 400, "Gvardiol", 2, 2)}, {**_el(5, 500, "Timber", 1, 2)}, {**_el(650, 777, "NewSigning", 2, 3)},
    {**_el(600, 999, "SomeoneElse", 1, 3)},
], "events": [{"id": 8, "is_current": True, "finished": True}, {"id": 9, "is_next": True}]}

GAME = {"current_event": 8, "current_event_finished": True, "next_event": 9}

ENTRIES = [  # league_entries: `id` = league_entry, `entry_id` = the manager's draft entry
    {"id": 2490, "entry_id": 2489, "entry_name": "Kurek XI", "player_first_name": "Hamit", "player_last_name": "Kurek"},
    {"id": 38779, "entry_id": 38724, "entry_name": "UpTheBoro", "player_first_name": "Amer", "player_last_name": "Hussain"},
]
H2H_DETAILS = {
    "league": {"id": 1000, "name": "Draft Mates", "scoring": "h", "transaction_mode": "waivers"},
    "league_entries": ENTRIES,
    "standings": [
        {"league_entry": 38779, "rank": 1, "total": 18, "matches_won": 6, "matches_drawn": 0,
         "matches_lost": 2, "points_for": 400, "points_against": 350},
        {"league_entry": 2490, "rank": 2, "total": 12, "matches_won": 4, "matches_drawn": 0,
         "matches_lost": 4, "points_for": 380, "points_against": 390},
    ],
    "matches": [
        {"event": 8, "league_entry_1": 2490, "league_entry_1_points": 48, "league_entry_2": 38779,
         "league_entry_2_points": 39, "started": True, "finished": True},
        {"event": 9, "league_entry_1": 38779, "league_entry_1_points": 0, "league_entry_2": 2490,
         "league_entry_2_points": 0, "started": False, "finished": False},
    ],
}
POINTS_DETAILS = {**H2H_DETAILS, "league": {**H2H_DETAILS["league"], "scoring": "c"}, "matches": [],
                  "standings": [{"league_entry": 2490, "rank": 1, "total": 358, "event_total": 48},
                                {"league_entry": 38779, "rank": 2, "total": 330, "event_total": 22}]}
ELEMENT_STATUS = [  # owner = draft entry_id
    {"element": 1, "owner": 2489, "status": "o", "in_accepted_trade": False},
    {"element": 3, "owner": 38724, "status": "o", "in_accepted_trade": False},
    {"element": 2, "owner": None, "status": "a", "in_accepted_trade": False},
    {"element": 4, "owner": None, "status": "a", "in_accepted_trade": False},
    {"element": 5, "owner": None, "status": "a", "in_accepted_trade": False},
    {"element": 600, "owner": None, "status": "a", "in_accepted_trade": False},
]
PICKS = {"picks": [{"element": 1, "position": 1, "multiplier": 1, "is_captain": False},
                   {"element": 3, "position": 11, "multiplier": 1, "is_captain": False},
                   {"element": 600, "position": 12, "multiplier": 1, "is_captain": False}],
         "entry_history": {}, "subs": [{"element_in": 600, "element_out": 3, "event": 8}]}
LIVE = {"elements": {"1": {"stats": {"total_points": 6}}, "3": {"stats": {"total_points": 2}},
                     "600": {"stats": {"total_points": 5}}}}
HISTORY = {"entry": {"name": "Kurek XI"},
           "history": [{"event": 8, "points": 48, "total_points": 358, "points_on_bench": 5}]}
TRANSACTIONS = {"transactions": [
    {"added": "2026-08-29T07:49:07Z", "element_in": 2, "element_out": 4, "entry": 2489, "event": 3,
     "kind": "w", "result": "a"},
    {"added": "2026-08-29T07:49:08Z", "element_in": 2, "element_out": 5, "entry": 38724, "event": 3,
     "kind": "w", "result": "do"},
    {"added": "2026-09-02T10:03:13Z", "element_in": 600, "element_out": 5, "entry": 38724, "event": 4,
     "kind": "f", "result": "a"},
]}
TRADES = {"trades": [{"event": 3, "offered_entry": 2489, "received_entry": 38724, "state": "p",
                      "offer_time": "2026-08-30T13:39:40Z",
                      "tradeitem_set": [{"element_in": 3, "element_out": 1}]}]}
CHOICES = {"choices": [  # index = overall pick; pick = within the round (snake draft)
    {"index": 3, "pick": 1, "round": 2, "entry": 2489, "entry_name": "Kurek XI", "element": 2, "was_auto": True},
    {"index": 2, "pick": 2, "round": 1, "entry": 2489, "entry_name": "Kurek XI", "element": 1, "was_auto": False},
    {"index": 1, "pick": 1, "round": 1, "entry": 38724, "entry_name": "UpTheBoro", "element": 3, "was_auto": False},
]}
PUBLIC = {"entry": {"id": 2489, "name": "Kurek XI", "player_first_name": "Hamit",
                    "player_last_name": "Kurek", "league_set": [1000]}}


@pytest.fixture
def draft_api_fake(monkeypatch):
    details = {"value": H2H_DETAILS}
    paths = {
        "entry/2489/event/8": PICKS, "event/8/live": LIVE, "entry/2489/history": HISTORY,
        "entry/2489/public": PUBLIC, "draft/league/1000/transactions": TRANSACTIONS,
        "draft/league/1000/trades": TRADES, "draft/1000/choices": CHOICES,
    }

    async def fetch_draft(path):
        return paths[path]

    async def boot(force=False):
        return DRAFT_BOOTSTRAP

    async def classic_boot(force=False):
        return CLASSIC_BOOTSTRAP

    async def game():
        return GAME

    async def league(lid):
        return details["value"]

    async def status(lid):
        return ELEMENT_STATUS

    async def fetch_fpl(path):
        if path.startswith("fixtures/"):
            return [{"team_h": 1, "team_a": 2, "team_h_difficulty": 4, "team_a_difficulty": 3,
                     "started": False}]
        if path.startswith("event/"):
            return {"elements": [{"id": 650, "stats": {"total_points": 5, "starts": 1, "minutes": 90}},
                                 {"id": 1, "stats": {"total_points": 6, "starts": 1, "minutes": 90}}]}
        raise AssertionError(path)

    for name, fn in (("fetch_draft", fetch_draft), ("fetch_draft_bootstrap", boot),
                     ("fetch_draft_game", game), ("fetch_draft_league", league),
                     ("fetch_draft_element_status", status), ("fetch_fpl", fetch_fpl),
                     ("fetch_fpl_bootstrap", classic_boot)):
        monkeypatch.setattr(ft, name, fn)
    return details


def _caller(**fpl):
    return Client("t", "T", "0" * 64, frozenset({"fpl"}), settings={"fpl": fpl})


@pytest.fixture
def draft_caller():
    with acting_as(_caller(draft_entry_id=2489, draft_league_id=1000), None):
        yield


def run(coro):
    return asyncio.run(coro)


# ── game resolution ──────────────────────────────────────────────────────────

def test_resolve_game():
    with acting_as(_caller(entry_id=1, league_id=2), None):
        assert identity.resolve_game(None) == "classic"
    with acting_as(_caller(draft_entry_id=3, draft_league_id=4), None):
        assert identity.resolve_game(None) == "draft"
        assert identity.resolve_game("classic") == "classic"
    with acting_as(_caller(entry_id=1, draft_entry_id=3), None):
        out = identity.resolve_game(None)
        assert isinstance(out, dict) and "game=" in out["Tip"]
        assert identity.resolve_game("draft") == "draft"
    with acting_as(_caller(), None):
        assert identity.resolve_game(None) == "classic"  # no defaults: explicit ids stay classic


def test_both_games_without_game_asks(draft_api_fake):
    with acting_as(_caller(entry_id=1, league_id=2, draft_entry_id=2489, draft_league_id=1000), None):
        assert "both" in run(ft.get_my_fpl_squad())["Error"]


# ── translations ─────────────────────────────────────────────────────────────

def test_draft_players_map_to_classic_by_code():
    mapping = draft_api.classic_ids_by_draft_id(DRAFT_BOOTSTRAP, CLASSIC_BOOTSTRAP)
    assert mapping[600] == 650 and mapping[3] == 3  # not 600 → SomeoneElse


def test_draft_league_indexes_both_ids():
    league = draft_api.DraftLeague(H2H_DETAILS)
    assert league.by_league_entry_id(38779)["team_name"] == "UpTheBoro"
    assert league.by_entry(38724)["manager"] == "Amer Hussain"
    assert [e["entry_id"] for e in league.find_manager("kurek")] == [2489]


# ── tools, draft path ────────────────────────────────────────────────────────

def test_h2h_standings_and_upcoming_matches(draft_api_fake, draft_caller):
    out = run(ft.get_fpl_league_standings())
    assert out["game"] == "draft" and out["scoring"] == "head-to-head"
    assert [r["team_name"] for r in out["standings"]] == ["UpTheBoro", "Kurek XI"]
    assert out["standings"][1]["is_me"] and out["standings"][1]["won"] == 4
    assert out["matches_gameweek"] == 9  # GW8 finished → the next one's matches
    assert out["matches"][0]["involves_me"] and "score" not in out["matches"][0]


def test_points_league_standings(draft_api_fake, draft_caller):
    draft_api_fake["value"] = POINTS_DETAILS
    out = run(ft.get_fpl_league_standings())
    assert out["scoring"] == "points" and "matches" not in out
    assert out["standings"][0] == {"rank": 1, "team_name": "Kurek XI", "manager": "Hamit Kurek",
                                   "is_me": True, "total_points": 358, "gameweek_points": 48}


def test_draft_squad(draft_api_fake, draft_caller):
    out = run(ft.get_my_fpl_squad())
    assert out["game"] == "draft" and out["gameweek"] == 8 and out["team_name"] == "Kurek XI"
    squad = {p["name"]: p for p in out["squad"]}
    assert squad["NewSigning"]["on_bench"] and not squad["Haaland"]["on_bench"]
    assert squad["Raya"]["gameweek_points"] == 6 and "price" not in squad["Raya"]
    assert out["automatic_subs"] == [{"in": "NewSigning", "out": "Haaland"}]
    assert out["summary"]["gameweek_points"] == 48


def test_draft_leagues(draft_api_fake, draft_caller):
    out = run(ft.get_my_fpl_leagues())
    assert out["game"] == "draft"
    assert out["leagues"] == [{"id": 1000, "name": "Draft Mates", "scoring": "head-to-head", "my_rank": 2}]


def test_draft_owner_and_free_player(draft_api_fake, draft_caller):
    owned = run(ft.who_owns_player_in_league("Haaland"))
    assert owned["owned"] and owned["owner"]["team_name"] == "UpTheBoro" and not owned["owner"]["is_me"]
    free = run(ft.who_owns_player_in_league("Saliba"))
    assert not free["owned"] and free["available_via"] == "waivers"


def test_free_agents(draft_api_fake, draft_caller):
    out = run(ft.get_draft_free_agents(position="DEF"))
    # Timber (injured) is left out; Haaland/Raya are owned; sorted by form
    assert [p["name"] for p in out["players"]] == ["Gvardiol", "Saliba"]
    assert out["players"][1]["next_fixtures"] == [{"opponent": "MCI", "home_or_away": "H", "difficulty": 4}]
    with_injured = run(ft.get_draft_free_agents(position="DEF", include_unavailable=True))
    assert with_injured["players"][0]["name"] == "Timber"


def test_activity_transactions_trades_and_draft(draft_api_fake, draft_caller):
    tx = run(ft.get_draft_activity())
    assert [t["player_in"] for t in tx["items"]] == ["NewSigning (MCI)", "Saliba (ARS)"]  # newest first
    assert tx["items"][0]["type"] == "free agent"
    denied = run(ft.get_draft_activity(include_denied=True, gameweek=3))
    assert {t["result"].split(" ")[0] for t in denied["items"]} == {"accepted", "denied"}
    mine = run(ft.get_draft_activity(manager="kurek"))
    assert [t["manager"] for t in mine["items"]] == ["Hamit Kurek"]

    trade = run(ft.get_draft_activity(kind="trades"))["items"][0]
    assert trade["offered_by_team"] == "Kurek XI" and trade["offered_by_gets"] == ["Haaland (MCI)"]
    assert trade["offered_to_gets"] == ["Raya (ARS)"] and trade["state"] == "processed"

    picks = run(ft.get_draft_activity(kind="draft"))["items"]
    assert [(p["pick"], p["round"], p["pick_in_round"], p["player"]) for p in picks] == [
        (1, 1, 1, "Haaland (MCI)"), (2, 1, 2, "Raya (ARS)"), (3, 2, 1, "Saliba (ARS)")]


def test_live_follows_the_draft_squad(draft_api_fake, draft_caller):
    out = run(ft.get_fpl_live(gameweek=8))
    mine = out["my_players"]
    assert mine["game"] == "draft"
    by_name = {p["name"]: p for p in mine["players"]}
    assert by_name["NewSigning"]["points"] == 5 and by_name["NewSigning"]["on_bench"]  # via code → 650
    assert by_name["NewSigning"]["points_counted"] == 0
    assert mine["provisional_gameweek_points"] == 6  # Raya 6 + Haaland 0 (not in live) ; bench excluded


def test_classic_only_tools_explain_for_draft_players(draft_caller):
    assert "classic" in run(ft.get_league_ownership())["message"]
    assert "classic" in run(ft.get_league_history())["message"]


def test_missing_draft_ids_say_so():
    with acting_as(_caller(draft_entry_id=5), None):
        assert "Draft league" in run(ft.get_draft_free_agents())["Error"]
    with acting_as(_caller(draft_league_id=5), None):
        assert "Draft team" in run(ft.get_my_fpl_squad())["Error"]


def test_free_agents_take_expected_points_from_classic(draft_api_fake, draft_caller, monkeypatch):
    # the draft API has no ep_next; classic does (matched by code, so NewSigning 600 → 650)
    classic = {**CLASSIC_BOOTSTRAP, "elements": [
        {**e, "ep_next": {650: "8.5", 2: "4.0", 4: "1.0"}.get(e["id"], "0.0")} for e in CLASSIC_BOOTSTRAP["elements"]]}
    draft = {**DRAFT_BOOTSTRAP, "elements": [{**e, "ep_next": None} for e in DRAFT_BOOTSTRAP["elements"]]}

    async def classic_boot(force=False):
        return classic

    async def draft_boot(force=False):
        return draft

    monkeypatch.setattr(ft, "fetch_fpl_bootstrap", classic_boot)
    monkeypatch.setattr(ft, "fetch_draft_bootstrap", draft_boot)
    out = run(ft.get_draft_free_agents(sort_by="expected_points", limit=3))
    assert [(p["name"], p["expected_points"]) for p in out["players"]] == [
        ("NewSigning", "8.5"), ("Saliba", "4.0"), ("Gvardiol", "1.0")]


def test_empty_draft_picks_are_explained(draft_api_fake, draft_caller, monkeypatch):
    real = ft.fetch_draft

    async def no_choices(path):
        return {"choices": []} if path.startswith("draft/1000/choices") else await real(path)

    monkeypatch.setattr(ft, "fetch_draft", no_choices)
    out = run(ft.get_draft_activity(kind="draft"))
    assert out["items"] == [] and "no draft picks" in out["message"]


def test_live_before_the_gameweek_starts_explains_instead_of_404(draft_api_fake, draft_caller, monkeypatch):
    async def nothing_started(force=False):
        return {**CLASSIC_BOOTSTRAP, "events": [{"id": 8, "is_current": True, "finished": True},
                                                {"id": 9, "is_next": True, "finished": False}]}

    monkeypatch.setattr(ft, "fetch_fpl_bootstrap", nothing_started)
    out = run(ft.get_fpl_live())
    assert out["gameweek"] == 9 and not out["any_live"]
    assert "has not started" in out["my_players"]["note"]

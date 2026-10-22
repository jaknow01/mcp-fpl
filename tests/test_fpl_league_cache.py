"""Unit tests for the FPL mini-league squad cache ladder (no network / DB needed).

`load_league_squads` orchestrates: in-process L1 memo -> Postgres read-through -> live API
scan (write-through). We drive each branch by monkeypatching the DB helpers and the raw API
scanner at the module level, so the tests never touch a database or the FPL API."""
import asyncio

import pytest

from mcp_fpl import fpl_api


@pytest.fixture(autouse=True)
def _clear_memo():
    """Each test starts with an empty in-process league memo."""
    fpl_api._FPL_LEAGUE_CACHE.clear()
    yield
    fpl_api._FPL_LEAGUE_CACHE.clear()


def _pairs():
    return [
        ({"entry": 1, "player_name": "A", "entry_name": "AA", "rank": 1}, [{"element": 10}]),
        ({"entry": 2, "player_name": "B", "entry_name": "BB", "rank": 2}, [{"element": 20}]),
    ]


def test_cache_miss_scans_api_and_writes_through(monkeypatch):
    saved = {}
    scan_calls = {"n": 0}

    async def fake_scan(league_id, gw, max_managers):
        scan_calls["n"] += 1
        return "My League", _pairs(), True

    monkeypatch.setattr(fpl_api, "_load_league_squads_from_db", lambda *a, **k: None)
    monkeypatch.setattr(
        fpl_api, "_save_league_squads_to_db",
        lambda lid, gw, name, mx, complete, pairs: saved.update(
            league_id=lid, gw=gw, name=name, complete=complete, pairs=pairs
        ),
    )
    monkeypatch.setattr(fpl_api, "_scan_league_squads", fake_scan)

    name, pairs = asyncio.run(fpl_api.load_league_squads(123, 5, 50))
    assert name == "My League"
    assert [p[0]["entry"] for p in pairs] == [1, 2]
    assert scan_calls["n"] == 1
    # write-through happened, tuples serialized to two-item lists
    assert saved["league_id"] == 123 and saved["gw"] == 5 and saved["complete"] is True
    # load_league_squads hands the raw (row, picks) pairs to the writer, which is what
    # serializes tuples -> lists internally; the writer sees the pairs as produced by the scan.
    assert [p[0]["entry"] for p in saved["pairs"]] == [1, 2]


def test_l1_memo_hit_avoids_second_scan(monkeypatch):
    scan_calls = {"n": 0}

    async def fake_scan(league_id, gw, max_managers):
        scan_calls["n"] += 1
        return "My League", _pairs(), True

    monkeypatch.setattr(fpl_api, "_load_league_squads_from_db", lambda *a, **k: None)
    monkeypatch.setattr(fpl_api, "_save_league_squads_to_db", lambda *a, **k: None)
    monkeypatch.setattr(fpl_api, "_scan_league_squads", fake_scan)

    asyncio.run(fpl_api.load_league_squads(123, 5, 50))
    asyncio.run(fpl_api.load_league_squads(123, 5, 50))  # served from L1 memo
    assert scan_calls["n"] == 1


def test_db_hit_serves_without_scanning(monkeypatch):
    scan_calls = {"n": 0}

    async def fake_scan(*a, **k):
        scan_calls["n"] += 1
        return "x", [], True

    monkeypatch.setattr(
        fpl_api, "_load_league_squads_from_db",
        lambda lid, gw, age, mn: {
            "league_name": "Cached League", "max_managers": 50, "complete": True,
            "pairs": [[{"entry": 9, "rank": 1}, [{"element": 7}]]],
        },
    )
    monkeypatch.setattr(fpl_api, "_scan_league_squads", fake_scan)

    name, pairs = asyncio.run(fpl_api.load_league_squads(123, 5, 50))
    assert name == "Cached League"
    assert pairs[0][0]["entry"] == 9
    assert scan_calls["n"] == 0  # DB hit — no API scan


def test_force_bypasses_both_caches(monkeypatch):
    db_reads = {"n": 0}
    scan_calls = {"n": 0}

    def fake_db_read(*a, **k):
        db_reads["n"] += 1
        return {"league_name": "Cached", "max_managers": 50, "complete": True, "pairs": []}

    async def fake_scan(league_id, gw, max_managers):
        scan_calls["n"] += 1
        return "Fresh League", _pairs(), True

    monkeypatch.setattr(fpl_api, "_load_league_squads_from_db", fake_db_read)
    monkeypatch.setattr(fpl_api, "_save_league_squads_to_db", lambda *a, **k: None)
    monkeypatch.setattr(fpl_api, "_scan_league_squads", fake_scan)

    # prime the memo, then force a rescan
    asyncio.run(fpl_api.load_league_squads(123, 5, 50, force=True))
    name, _ = asyncio.run(fpl_api.load_league_squads(123, 5, 50, force=True))
    assert name == "Fresh League"
    assert db_reads["n"] == 0   # force skips the DB read
    assert scan_calls["n"] == 2  # both calls rescanned


def test_partial_memo_insufficient_for_larger_request_rescans(monkeypatch):
    scan_calls = {"n": 0}

    async def fake_scan(league_id, gw, max_managers):
        scan_calls["n"] += 1
        # incomplete league capped at the requested size
        return "Big League", _pairs(), False

    monkeypatch.setattr(fpl_api, "_load_league_squads_from_db", lambda *a, **k: None)
    monkeypatch.setattr(fpl_api, "_save_league_squads_to_db", lambda *a, **k: None)
    monkeypatch.setattr(fpl_api, "_scan_league_squads", fake_scan)

    asyncio.run(fpl_api.load_league_squads(123, 5, 25))        # memo: max=25, complete=False
    asyncio.run(fpl_api.load_league_squads(123, 5, 50))        # wants more -> rescan
    assert scan_calls["n"] == 2


def _standings_page(results, has_next):
    return {"league": {"name": "L"}, "standings": {"results": results, "has_next": has_next}}


def test_scan_marks_complete_when_whole_league_fits(monkeypatch):
    async def fake_fetch(path):
        if path.startswith("leagues-classic"):
            return _standings_page(
                [{"entry": 1, "rank": 1}, {"entry": 2, "rank": 2}], has_next=False
            )
        return {"picks": [{"element": 1}]}  # entry/.../picks/

    monkeypatch.setattr(fpl_api, "fetch_fpl", fake_fetch)
    name, pairs, complete = asyncio.run(fpl_api._scan_league_squads(1, 5, 50))
    assert complete is True and len(pairs) == 2


def test_scan_marks_incomplete_when_capped(monkeypatch):
    async def fake_fetch(path):
        if path.startswith("leagues-classic"):
            # more managers than the cap, and more pages available
            return _standings_page(
                [{"entry": i, "rank": i} for i in range(1, 6)], has_next=True
            )
        return {"picks": []}

    monkeypatch.setattr(fpl_api, "fetch_fpl", fake_fetch)
    name, pairs, complete = asyncio.run(fpl_api._scan_league_squads(1, 5, 3))
    assert complete is False and len(pairs) == 3  # capped at max_managers

"""fpl_error(): failed FPL calls become actionable tool results (hint + calendar)."""
import requests

from mcp_fpl.fpl_api import fpl_error, gameweek_calendar

_BOOTSTRAP = {
    "events": [
        {"id": 4, "is_current": True, "is_next": False, "finished": True,
         "deadline_time": "2026-09-12T10:00:00Z"},
        {"id": 5, "is_current": False, "is_next": True, "finished": False,
         "deadline_time": "2026-09-19T10:00:00Z"},
    ]
}


def _http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status} Client Error", response=resp)


def test_gameweek_calendar():
    cal = gameweek_calendar(_BOOTSTRAP)
    assert cal["current"] == {"gameweek": 4, "deadline_time": "2026-09-12T10:00:00Z", "finished": True}
    assert cal["next"]["gameweek"] == 5
    assert gameweek_calendar(None) is None


def test_404_with_gameweek_explains_deadline_and_includes_calendar():
    out = fpl_error("Error while getting the FPL squad", _http_error(404),
                    bootstrap=_BOOTSTRAP, gameweek=5)
    assert out["Message"] == "Error while getting the FPL squad"
    assert "404" in out["Error"]
    assert "gameweek 5" in out["Hint"] and "deadline" in out["Hint"]
    assert out["gameweek_calendar"]["current"]["gameweek"] == 4


def test_404_without_gameweek_points_at_the_id():
    out = fpl_error("Error while getting the league standings", _http_error(404))
    assert "id" in out["Hint"]
    assert "gameweek_calendar" not in out


def test_unavailable_api_says_not_to_retry():
    for exc in (_http_error(503), requests.Timeout("slow")):
        assert "temporarily unavailable" in fpl_error("x", exc)["Hint"]


def test_rate_limit_hint():
    assert "rate-limiting" in fpl_error("x", _http_error(429))["Hint"]

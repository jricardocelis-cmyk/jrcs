"""Parsers de API-Football contra payloads con la forma documentada (no contra respuestas reales)."""
from goalalert.models import AWAY, HOME, NONE, NO, OVER, UNDER, YES, Market
import pytest

from goalalert.providers.apifootball import ApiFootball
from goalalert.providers.apifootball import (
    DEFAULT_BET_NAMES, _apply_extra, _reverse_bet_names, count_red_cards, parse_live_fixture,
    parse_live_odds, parse_result, parse_statistics,
)

NAMES = _reverse_bet_names(DEFAULT_BET_NAMES)


def fixture_item(events=None, status=("2H", 63)):
    item = {
        "fixture": {"id": 55, "status": {"short": status[0], "elapsed": status[1]}},
        "league": {"id": 281, "name": "Liga 1", "country": "Peru"},
        "teams": {"home": {"id": 10, "name": "Alianza"}, "away": {"id": 20, "name": "Cristal"}},
        "goals": {"home": 1, "away": 0},
    }
    if events is not None:
        item["events"] = events
    return item


def test_live_fixture_with_events_counts_reds_per_team():
    events = [
        {"type": "Goal", "detail": "Normal Goal", "team": {"id": 10}},
        {"type": "Card", "detail": "Yellow Card", "team": {"id": 10}},
        {"type": "Card", "detail": "Red Card", "team": {"id": 20}},
        {"type": "Card", "detail": "Second Yellow card", "team": {"id": 20}},
    ]
    m = parse_live_fixture(fixture_item(events))
    assert (m.fixture_id, m.league_id, m.status, m.elapsed) == (55, 281, "2H", 63)
    assert (m.home.goals, m.away.goals, m.home.red_cards, m.away.red_cards) == (1, 0, 0, 2)
    assert m.red_known and m.home.team_id == 10


def test_live_fixture_without_events_marks_reds_unknown():
    m = parse_live_fixture(fixture_item(events=None))
    assert not m.red_known and m.home.red_cards == 0


def test_statistics_and_enrichment():
    m = parse_live_fixture(fixture_item(events=None))
    stats = parse_statistics([
        {"team": {"id": 10}, "statistics": [
            {"type": "Total Shots", "value": 11}, {"type": "Shots on Goal", "value": 5},
            {"type": "expected_goals", "value": "1.32"}]},
        {"team": {"id": 20}, "statistics": [
            {"type": "Total Shots", "value": None}, {"type": "Shots on Goal", "value": 1}]},
    ], m)
    out = _apply_extra(m, stats, reds=(0, 1))
    assert (out.home.shots, out.home.shots_on_target, out.home.xg) == (11, 5, 1.32)
    assert (out.away.shots, out.away.shots_on_target, out.away.xg) == (None, 1, None)
    assert out.away.red_cards == 1 and out.red_known


def test_count_red_cards_helper():
    m = parse_live_fixture(fixture_item())
    assert count_red_cards([{"type": "Card", "detail": "Red Card", "team": {"id": 10}}], m) == (1, 0)


def odds_item(**over):
    item = {
        "fixture": {"id": 55, "status": {"elapsed": 63}},
        "teams": {"home": {"id": 10, "goals": 1}, "away": {"id": 20, "goals": 0}},
        "status": {"stopped": False, "blocked": False, "finished": False},
        "update": "2026-10-22T20:15:00+00:00",
        "odds": [
            {"name": "Over/Under Line", "values": [
                {"value": "Over", "odd": "1.85", "handicap": "1.5", "suspended": False},
                {"value": "Under", "odd": "1.95", "handicap": "1.5", "suspended": False},
                {"value": "Over", "odd": "3.10", "handicap": "2", "suspended": False},   # línea entera: se ignora
                {"value": "Over", "odd": "2.10", "handicap": "2.25", "suspended": False},  # cuarto asiático: se ignora
            ]},
            {"name": "Both Teams to Score", "values": [
                {"value": "Yes", "odd": "2.20", "suspended": False},
                {"value": "No", "odd": "1.62", "suspended": True},
            ]},
            {"name": "Next Goal", "values": [
                {"value": "Home", "odd": "2.0", "handicap": "2"},
                {"value": "Away", "odd": "3.0", "handicap": "2"},
                {"value": "No goal", "odd": "5.0", "handicap": "2"},
                {"value": "Home", "odd": "9.0", "handicap": "3"},  # gol #3: se ignora
            ]},
            {"name": "Corners Over/Under", "values": [{"value": "Over", "odd": "1.9", "handicap": "9.5"}]},
        ],
    }
    item.update(over)
    return item


def test_parse_live_odds_maps_markets_and_filters():
    quotes = parse_live_odds(odds_item(), NAMES)
    ou = [q for q in quotes if q.market is Market.OVER_UNDER]
    assert {(q.selection, q.line, q.odds) for q in ou} == {(OVER, 1.5, 1.85), (UNDER, 1.5, 1.95)}
    btts = {q.selection: q for q in quotes if q.market is Market.BTTS}
    assert not btts[YES].suspended and btts[NO].suspended
    ng = [q for q in quotes if q.market is Market.NEXT_GOAL]
    assert {q.selection for q in ng} == {HOME, AWAY, NONE} and len(ng) == 3
    assert all(q.fixture_id == 55 and q.updated_at for q in quotes)


def test_halted_fixture_suspends_everything():
    item = odds_item(status={"stopped": True, "blocked": False, "finished": False})
    assert all(q.suspended for q in parse_live_odds(item, NAMES))


def test_unknown_or_bad_odds_are_skipped():
    item = odds_item()
    item["odds"] = [{"name": "Both Teams to Score", "values": [{"value": "Yes", "odd": "n/a"}, {"value": "Maybe", "odd": "2.0"}]}]
    assert parse_live_odds(item, NAMES) == []


def result_item(short, ft=(2, 1), goals=(2, 1)):
    return {
        "fixture": {"id": 55, "status": {"short": short}},
        "goals": {"home": goals[0], "away": goals[1]},
        "score": {"fulltime": {"home": ft[0], "away": ft[1]}},
    }


def test_parse_result_uses_90_minute_score_and_handles_voids():
    r = parse_result(result_item("AET", ft=(1, 1), goals=(2, 1)))
    assert (r.finished, r.home_goals, r.away_goals) == (True, 1, 1)
    assert parse_result(result_item("FT")).finished
    assert not parse_result(result_item("2H")).finished
    assert parse_result(result_item("PST")).voided


def test_which_team_will_score_the_nth_goal_is_the_next_goal_only_when_n_is_total_plus_one():
    item = odds_item()  # marcador 1-0: el próximo gol es el 2.º
    item["odds"] = [
        {"name": "Which team will score the 1st goal?", "values": [{"value": "Home", "odd": "9.0"}]},
        {"name": "Which team will score the 2nd goal?", "values": [
            {"value": "Home", "odd": "2.1"}, {"value": "Away", "odd": "3.2"}, {"value": "No Goal", "odd": "6.0"}]},
        {"name": "Which team will score the 3rd goal?", "values": [{"value": "Away", "odd": "7.0"}]},
    ]
    quotes = parse_live_odds(item, NAMES)
    assert {(q.market, q.selection, q.odds) for q in quotes} == {
        (Market.NEXT_GOAL, HOME, 2.1), (Market.NEXT_GOAL, AWAY, 3.2), (Market.NEXT_GOAL, NONE, 6.0)}


def test_match_goals_is_not_mapped_until_its_values_are_confirmed():
    item = odds_item()
    item["odds"] = [{"name": "Match Goals", "values": [{"value": "Over 2.5", "odd": "1.9"}]}]
    assert parse_live_odds(item, NAMES) == []


def test_timestamps_without_zone_are_utc_not_local_time():
    from goalalert.providers.apifootball import _iso_to_epoch
    utc = _iso_to_epoch("2026-10-23T01:00:00+00:00")
    assert _iso_to_epoch("2026-10-23T01:00:00") == utc  # sin zona => UTC, no la hora del equipo
    assert _iso_to_epoch("2026-10-23T01:00:00Z") == utc
    assert _iso_to_epoch("2026-10-22T20:00:00-05:00") == utc
    assert _iso_to_epoch("basura") is None and _iso_to_epoch(None) is None


def test_quote_age_is_measured_against_the_server_clock_not_the_pc_clock():
    item = odds_item(update="2026-10-23T00:35:00+00:00")
    base = parse_live_odds(item, NAMES)[0].updated_at
    # el PC va 65 s adelantado respecto al servidor: updated_at pasa al reloj del PC sumando ese desfase
    assert parse_live_odds(item, NAMES, clock_skew=65.0)[0].updated_at == pytest.approx(base + 65.0)


def test_get_reads_the_server_date_header_and_computes_the_skew(monkeypatch):
    import io
    from email.utils import format_datetime
    from datetime import datetime, timezone
    from goalalert.providers import apifootball as mod

    server = datetime(2026, 10, 23, 0, 36, 10, tzinfo=timezone.utc)

    class Resp(io.BytesIO):
        headers = {"Date": format_datetime(server, usegmt=True), "x-ratelimit-requests-remaining": "90"}
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(mod, "urlopen", lambda req, timeout=None: Resp(b'{"errors": [], "response": [1]}'))
    monkeypatch.setattr(mod.time, "time", lambda: server.timestamp() + 62.0)  # el PC va 62 s adelantado
    api = ApiFootball("k")
    assert api._get("status") == [1]
    assert api.clock_skew_s == pytest.approx(62.0) and api.remaining_requests == 90


def test_upcoming_queries_one_day_at_a_time_without_the_next_parameter(monkeypatch):
    """El plan gratuito rechaza `next`: se consulta por fecha, hoy y los días siguientes, en hora de Lima."""
    import re
    calls = []

    def fake_get(self, path, **params):
        calls.append((path, params))
        day = params["date"]
        return [{"fixture": {"date": f"{day}T15:30:00-05:00", "status": {"short": "NS"}}, "league": {"name": "Primera División"},
                 "teams": {"home": {"name": "Alianza Lima"}, "away": {"name": "Sporting Cristal"}}}]

    monkeypatch.setattr(ApiFootball, "_get", fake_get)
    games = ApiFootball("k").upcoming(281, 3)
    assert len(calls) == 3 and all(path == "fixtures" and "next" not in p for path, p in calls)
    assert all(p["league"] == 281 and p["timezone"] == "America/Lima" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p["date"]) for _, p in calls)
    assert [p["date"] for _, p in calls] == sorted(p["date"] for _, p in calls) and len({p["date"] for _, p in calls}) == 3
    assert games[0]["home"] == "Alianza Lima" and games[0]["when"].endswith("15:30") and [g["when"] for g in games] == sorted(g["when"] for g in games)

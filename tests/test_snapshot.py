"""`snapshot` y `markets` de punta a punta con una API simulada (payloads con la forma documentada)."""
from pathlib import Path

import pytest

from goalalert.cli import main
from goalalert.providers.apifootball import ApiFootball

EXAMPLE = Path(__file__).parent.parent / "config.example.toml"

FIXTURE = {
    "fixture": {"id": 55, "status": {"short": "1H", "elapsed": 30}},
    "league": {"id": 281, "name": "Liga 1", "country": "Peru"},
    "teams": {"home": {"id": 10, "name": "Alianza"}, "away": {"id": 20, "name": "Cristal"}},
    "goals": {"home": 0, "away": 0},
    "events": [],
}
STATS = [
    {"team": {"id": 10}, "statistics": [{"type": "Total Shots", "value": 2}, {"type": "Shots on Goal", "value": 0}]},
    {"team": {"id": 20}, "statistics": [{"type": "Total Shots", "value": 3}, {"type": "Shots on Goal", "value": 1}]},
]
ODDS = {
    "fixture": {"id": 55, "status": {"elapsed": 30}},
    "teams": {"home": {"id": 10, "goals": 0}, "away": {"id": 20, "goals": 0}},
    "status": {"stopped": False, "blocked": False, "finished": False},
    "odds": [
        {"name": "Over/Under Line", "values": [
            {"value": "Over", "odd": "1.45", "handicap": "0.5"}, {"value": "Under", "odd": "3.10", "handicap": "0.5"},
            {"value": "Over", "odd": "2.40", "handicap": "1.5"}, {"value": "Under", "odd": "1.55", "handicap": "1.5"},
        ]},
        {"name": "Both Teams to Score", "values": [{"value": "Yes", "odd": "1.80"}]},  # incompleto a propósito
        {"name": "Which team will score the 1st goal?", "values": [
            {"value": "Home", "odd": "2.30"}, {"value": "Away", "odd": "3.10"}, {"value": "No Goal", "odd": "12.0"}]},
        {"name": "Match Goals", "values": [{"value": "Over 2.5", "odd": "1.90"}]},  # NO mapeado
        {"name": "Corners Over/Under", "values": [{"value": "Over", "odd": "1.9", "handicap": "9.5"}]},
    ],
}


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setenv("APIFOOTBALL_KEY", "clave-de-prueba")

    def fake_get(self, path, **params):
        return {
            "fixtures": [FIXTURE],
            "odds/live": [ODDS],
            "fixtures/statistics": STATS,
            "fixtures/events": [],
        }[path]

    monkeypatch.setattr(ApiFootball, "_get", fake_get)


def test_snapshot_shows_model_vs_market_and_the_reason_for_each_discard(fake_api, capsys):
    assert main(["snapshot", "-c", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "1 partidos en vivo · 1 con cuotas" in out
    assert "cuotas leídas: btts=1, next_goal=3, over_under=4" in out  # Match Goals y Corners no cuentan
    assert "tiros 2-3 · a puerta 0-1" in out
    assert "Liga 1 (Peru) · Alianza 0-0 Cristal · 1H 30'" in out
    alert = next(line for line in out.splitlines() if "Más de 0.5 goles" in line)
    assert "→ ALERTA" in alert and "@1.45" in alert
    assert "descartada: mercado incompleto" in out  # BTTS con una sola cuota
    assert "descartada: prob. combinada" in out  # Over/Under 1.5 no llega a 60%
    assert "Próximo gol: Alianza" in out  # «Which team will score the 1st goal?» con 0-0 es el próximo gol


def test_snapshot_without_stats_skips_the_extra_requests(fake_api, capsys, monkeypatch):
    calls = []
    original = ApiFootball._get

    def spy(self, path, **params):
        calls.append(path)
        return original(self, path, **params)

    monkeypatch.setattr(ApiFootball, "_get", spy)
    main(["snapshot", "-c", str(EXAMPLE), "--no-stats"])
    assert "fixtures/statistics" not in calls and "fixtures/events" not in calls
    assert "tiros n/d" in capsys.readouterr().out


def test_markets_lists_leagues_mapping_and_raw_samples(fake_api, capsys):
    assert main(["markets", "-c", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "281 · Liga 1 · Peru · 1" in out
    assert "  ✓ Over/Under Line (1)" in out and "    Match Goals (1)" in out  # solo el primero está mapeado
    assert "Muestra de valores crudos" in out
    assert '"value": "Over 2.5"' in out  # se ve cómo es «Match Goals» sin tener que adivinar
    assert "Corners Over/Under" not in out.split("Muestra de valores crudos")[1]


def test_snapshot_shows_numbers_and_the_age_of_stale_quotes_instead_of_hiding_them(fake_api, capsys, monkeypatch):
    stale = dict(ODDS, update="2020-01-01T00:00:00+00:00")
    original = ApiFootball._get
    monkeypatch.setattr(ApiFootball, "_get", lambda self, path, **p: [stale] if path == "odds/live" else original(self, path, **p))
    main(["snapshot", "-c", str(EXAMPLE)])
    out = capsys.readouterr().out
    assert "campo update de la API): 2020-01-01T00:00:00+00:00" in out
    assert "el agente las descartaría (límite 60 s)" in out
    assert "modelo 82% · mercado 68%" in out  # los números se ven aunque el agente real descartaría la cuota


def test_coverage_prints_what_the_league_declares(fake_api, capsys, monkeypatch):
    payload = [{"league": {"id": 281, "name": "Liga 1"}, "country": {"name": "Peru"}, "seasons": [
        {"year": 2025, "current": False, "coverage": {}},
        {"year": 2026, "current": True, "coverage": {"fixtures": {"events": True, "statistics_fixtures": False}, "odds": True}}]}]
    monkeypatch.setattr(ApiFootball, "_get", lambda self, path, **p: payload)
    assert main(["coverage", "-c", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "liga 281: Liga 1 (Peru) · temporada 2026" in out
    assert "estadísticas por partido (tiros, xG): False" in out and "eventos (goles, tarjetas):            True" in out

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
def league_cfg(tmp_path):
    p = tmp_path / "league.toml"
    p.write_text('[model]\nprior = "league"\n')
    return str(p)


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


def test_snapshot_shows_model_vs_market_and_the_reason_for_each_discard(fake_api, capsys, league_cfg):
    assert main(["snapshot", "-c", league_cfg]) == 0
    out = capsys.readouterr().out
    assert "1 partidos en vivo de las 32 ligas configuradas (de 1 en vivo en total) · 1 con cuotas" in out
    assert "cuotas leídas: btts=1, next_goal=3, over_under=4" in out  # Match Goals y Corners no cuentan
    assert "tiros 2-3 · a puerta 0-1" in out
    assert "Liga 1 (Peru) · Alianza 0-0 Cristal · 1H 30' · id 55" in out
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


def test_snapshot_shows_numbers_and_the_age_of_stale_quotes_instead_of_hiding_them(fake_api, capsys, monkeypatch, league_cfg):
    stale = dict(ODDS, update="2020-01-01T00:00:00+00:00")
    original = ApiFootball._get
    monkeypatch.setattr(ApiFootball, "_get", lambda self, path, **p: [stale] if path == "odds/live" else original(self, path, **p))
    main(["snapshot", "-c", league_cfg])
    out = capsys.readouterr().out
    assert "campo update de la API): 2020-01-01T00:00:00+00:00" in out
    assert "el agente las descartaría (límite 120 s)" in out
    assert "modelo 82% · mercado 68%" in out  # los números se ven aunque el agente real descartaría la cuota


def test_coverage_checks_every_configured_league_against_the_account(fake_api, capsys, monkeypatch):
    def entry(lid, name, country, stats):
        return {"league": {"id": lid, "name": name}, "country": {"name": country}, "seasons": [
            {"year": 2026, "current": True, "coverage": {"fixtures": {"events": True, "statistics_fixtures": stats}, "odds": True}}]}

    payload = [entry(2, "UEFA Champions League", "World", True),
               entry(39, "Premier League", "England", True),
               entry(281, "Primera División", "Peru", False),
               entry(140, "Copa Misteriosa", "Spain", True)]  # el id 140 debería ser La Liga: la API dice otra cosa
    monkeypatch.setattr(ApiFootball, "_get", lambda self, path, **p: payload)
    assert main(["coverage", "-c", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert any(l.strip().startswith("2 · UEFA Champions League (World) · temporada 2026 · estadísticas True") for l in lines)
    assert any("281 · Primera División (Peru)" in l and "estadísticas False" in l for l in lines)
    assert any("140 · Copa Misteriosa" in l and "⚠ se esperaba algo como «la liga»" in l for l in lines)
    assert "no figura entre las ligas con temporada en curso" in out  # los ids populares que la cuenta no devolvió
    assert "3 de 32 ligas declaran estadísticas (tiros, xG) y cuotas" in out and "(3 declaran estadísticas)" in out


def test_coverage_accepts_explicit_leagues(fake_api, capsys, monkeypatch):
    payload = [{"league": {"id": 13, "name": "CONMEBOL Libertadores"}, "country": {"name": "World"}, "seasons": [
        {"year": 2026, "current": True, "coverage": {"fixtures": {"events": True, "statistics_fixtures": True}, "odds": True}}]}]
    monkeypatch.setattr(ApiFootball, "_get", lambda self, path, **p: payload)
    assert main(["coverage", "-c", str(EXAMPLE), "--league", "13"]) == 0
    out = capsys.readouterr().out
    assert "13 · CONMEBOL Libertadores (World) · temporada 2026 · estadísticas True" in out and "1 de 1 ligas declaran estadísticas (tiros, xG) y cuotas" in out


def test_schedule_prints_the_games_of_the_configured_leagues(fake_api, capsys, monkeypatch):
    def game(lid, name, country, home):
        return {"fixture": {"id": 777, "date": "2026-10-24T15:30:00-05:00", "status": {"short": "NS"}},
                "league": {"id": lid, "name": name, "country": country}, "teams": {"home": {"name": home}, "away": {"name": "Rival"}}}

    monkeypatch.setattr(ApiFootball, "_get", lambda self, path, **p: [game(2, "UEFA Champions League", "World", "Real Madrid"),
                                                                       game(999, "Liga Ajena", "X", "Otro")])
    assert main(["schedule", "-c", str(EXAMPLE), "--days", "1"]) == 0
    out = capsys.readouterr().out
    assert "2026-10-24 15:30 (hora de Lima) · UEFA Champions League (World) · Real Madrid vs Rival · NS · id 777" in out
    assert "Otro" not in out  # fuera de las ligas configuradas



def test_snapshot_with_market_prior_shows_the_anchor_and_stays_close_to_the_market(fake_api, capsys):
    assert main(["snapshot", "-c", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "ancla de mercado: local" in out and "(total: over/under 1.5 · reparto: próximo gol)" in out
    assert "goles esperados restantes tras ajustar por presión" in out
    assert "desfase del reloj de tu PC respecto al servidor" in out
    assert "→ ALERTA" not in out  # sin evidencia en vivo que contradiga al mercado no hay nada que alertar


def test_snapshot_without_any_usable_market_says_why_it_skips_the_match(fake_api, capsys, monkeypatch):
    bare = dict(ODDS, odds=[ODDS["odds"][1]])  # solo «ambos anotan» incompleto
    original = ApiFootball._get
    monkeypatch.setattr(ApiFootball, "_get", lambda self, path, **p: [bare] if path == "odds/live" else original(self, path, **p))
    main(["snapshot", "-c", str(EXAMPLE)])
    assert "sin ancla de mercado" in capsys.readouterr().out


def test_api_errors_are_a_clear_message_not_a_traceback(fake_api, capsys, monkeypatch):
    from goalalert.providers import ProviderError

    def refuse(self, path, **params):
        raise ProviderError("fixtures: {'plan': 'Free plans do not have access to the Next parameter.'}")

    monkeypatch.setattr(ApiFootball, "_get", refuse)
    assert main(["schedule", "-c", str(EXAMPLE)]) == 1
    err = capsys.readouterr().err
    assert "error de la API: fixtures: {'plan'" in err and "Tu plan de API-Football no incluye esta consulta" in err
    assert "Traceback" not in err


def test_snapshot_flags_odds_that_lag_behind_the_score(fake_api, capsys, monkeypatch):
    lagging = dict(ODDS, teams={"home": {"id": 10, "goals": 1}, "away": {"id": 20, "goals": 0}})  # el partido está 0-0
    original = ApiFootball._get
    monkeypatch.setattr(ApiFootball, "_get", lambda self, path, **p: [lagging] if path == "odds/live" else original(self, path, **p))
    main(["snapshot", "-c", str(EXAMPLE)])
    out = capsys.readouterr().out
    assert "las cuotas son de otro marcador (1-0 en las cuotas, 0-0 en el partido)" in out
    assert "no evalúa este partido" in out and "ancla de mercado" not in out


def test_coverage_counts_only_leagues_with_both_stats_and_odds_as_able_to_alert(fake_api, capsys, monkeypatch):
    def entry(lid, name, stats, odds):
        return {"league": {"id": lid, "name": name}, "country": {"name": "X"}, "seasons": [
            {"year": 2026, "current": True, "coverage": {"fixtures": {"events": True, "statistics_fixtures": stats}, "odds": odds}}]}

    payload = [entry(2, "UEFA Champions League", True, True), entry(1, "World Cup", True, False), entry(143, "Copa del Rey", False, False)]
    monkeypatch.setattr(ApiFootball, "_get", lambda self, path, **p: payload)
    main(["coverage", "-c", str(EXAMPLE), "--league", "2", "--league", "1", "--league", "143"])
    out = capsys.readouterr().out
    assert "1 de 3 ligas declaran estadísticas (tiros, xG) y cuotas" in out and "(2 declaran estadísticas)" in out


def test_fixture_command_exercises_confirmation_and_settlement(fake_api, capsys, monkeypatch):
    finished = dict(FIXTURE, fixture={"id": 55, "status": {"short": "FT", "elapsed": 90}}, goals={"home": 2, "away": 1},
                    score={"fulltime": {"home": 2, "away": 1}})  # un partido terminado trae el mismo cuerpo, ya con el marcador final
    served = {"fixtures": [FIXTURE], "odds/live": [ODDS]}
    monkeypatch.setattr(ApiFootball, "_get", lambda self, path, **p: served[path])
    assert main(["fixture", "-c", str(EXAMPLE), "--id", "55"]) == 0
    out = capsys.readouterr().out
    assert "partido 55: Alianza 0-0 Cristal · 1H 30' · rojas 0-0" in out
    assert "cuotas re-consultadas (reconfirmación): btts=1, next_goal=3, over_under=4" in out
    assert "resultado: el partido aún no termina" in out

    served["fixtures"] = [finished]
    main(["fixture", "-c", str(EXAMPLE), "--id", "55"])
    assert "resultado: terminó 2-1 a los 90'" in capsys.readouterr().out

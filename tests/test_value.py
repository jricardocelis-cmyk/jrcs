import math
import pytest

from goalalert.model import MatchProbs
from goalalert.models import HOME, AWAY, NO, OVER, UNDER, YES, LiveMatch, Market, Quote, TeamLive
from goalalert.value import ValueConfig, find_signals, kelly_stake

NOW = 1_000_000.0
LAM = -math.log(1 - 0.78)  # P(al menos 1 gol restante) = 78%
PROBS = MatchProbs(0, 0, LAM / 2, LAM / 2)


def match(elapsed=30, status="1H"):
    return LiveMatch(1, 281, "Liga 1", "Peru", status, elapsed, TeamLive("A", 1), TeamLive("B", 2))


def ou(line, over, under, **kw):
    return [Quote(1, Market.OVER_UNDER, OVER, line, over, **kw), Quote(1, Market.OVER_UNDER, UNDER, line, under, **kw)]


def test_signal_when_model_beats_market_within_band():
    sigs = find_signals(match(), ou(0.5, 1.50, 2.90), PROBS, ValueConfig(), NOW)
    assert len(sigs) == 1
    s = sigs[0]
    assert (s.selection, s.line) == (OVER, 0.5)
    assert s.p_model == pytest.approx(0.78)
    assert s.p_model > s.p_final > s.p_market
    assert s.ev == pytest.approx(s.p_final * 1.50 - 1) and s.ev >= 0.06
    assert 0 < s.stake_pct <= ValueConfig().max_stake_pct
    # cuota mínima: a ese precio el EV combinado queda justo en el mínimo configurado
    assert s.p_final * s.min_odds - 1 == pytest.approx(ValueConfig().min_ev)
    assert 1 / s.p_final < s.min_odds < s.odds


def test_fair_or_worse_odds_give_no_signal():
    assert find_signals(match(), ou(0.5, 1.30, 3.60), PROBS, ValueConfig(), NOW) == []


def test_incomplete_market_is_skipped():
    only_over = ou(0.5, 1.50, 2.90)[:1]
    assert find_signals(match(), only_over, PROBS, ValueConfig(), NOW) == []


def test_suspended_and_stale_quotes_are_skipped():
    assert find_signals(match(), ou(0.5, 1.50, 2.90, suspended=True), PROBS, ValueConfig(), NOW) == []
    stale = ou(0.5, 1.50, 2.90, updated_at=NOW - 600)
    assert find_signals(match(), stale, PROBS, ValueConfig(), NOW) == []
    fresh = ou(0.5, 1.50, 2.90, updated_at=NOW - 5)
    assert len(find_signals(match(), fresh, PROBS, ValueConfig(), NOW)) == 1


def test_big_divergence_is_treated_as_bad_data():
    # el mercado dice ~50% y el modelo 78%: más de 15 pp => no se alerta
    assert find_signals(match(), ou(0.5, 1.90, 1.90), PROBS, ValueConfig(), NOW) == []


def test_minute_window_and_status():
    cfg = ValueConfig()
    q = ou(0.5, 1.50, 2.90)
    assert cfg.min_minute == 20
    assert find_signals(match(elapsed=19), q, PROBS, cfg, NOW) == []
    assert len(find_signals(match(elapsed=20), q, PROBS, cfg, NOW)) == 1
    assert find_signals(match(elapsed=88, status="2H"), q, PROBS, cfg, NOW) == []
    assert find_signals(match(status="HT", elapsed=45), q, PROBS, cfg, NOW) == []


def test_odds_band_and_min_prob():
    cfg = ValueConfig(min_odds=1.60)
    assert find_signals(match(), ou(0.5, 1.50, 2.90), PROBS, cfg, NOW) == []
    assert find_signals(match(), ou(0.5, 1.50, 2.90), PROBS, ValueConfig(min_prob=0.9), NOW) == []


def test_non_half_lines_ignored():
    assert find_signals(match(), ou(1.0, 1.50, 2.90), PROBS, ValueConfig(), NOW) == []


def test_btts_and_next_goal_use_complete_sets():
    probs = MatchProbs(1, 0, 0.9, 1.6)  # visita sin gol: BTTS Sí ~ 1-e^-1.6 = 79.8%
    btts = [Quote(1, Market.BTTS, YES, None, 1.45), Quote(1, Market.BTTS, NO, None, 2.90)]
    sigs = find_signals(match(), btts, probs, ValueConfig(min_ev=0.0, min_odds=1.3), NOW)
    assert [s.selection for s in sigs] == [YES]
    ng = [Quote(1, Market.NEXT_GOAL, HOME, None, 3.0), Quote(1, Market.NEXT_GOAL, AWAY, None, 2.0)]
    assert find_signals(match(), ng, probs, ValueConfig(), NOW) == []  # falta "ninguno"


def test_markets_filter():
    cfg = ValueConfig(markets=("btts",))
    assert find_signals(match(), ou(0.5, 1.50, 2.90), PROBS, cfg, NOW) == []


def test_kelly_stake_is_fractional_and_capped():
    assert kelly_stake(0.6, 2.0, 0.25, 0.05) == pytest.approx(0.25 * 0.2)
    assert kelly_stake(0.9, 3.0, 1.0, 0.02) == 0.02
    assert kelly_stake(0.4, 2.0, 0.25, 0.05) == 0.0


def test_signal_text_mentions_pick_and_odds():
    s = find_signals(match(), ou(0.5, 1.50, 2.90), PROBS, ValueConfig(), NOW)[0]
    text = s.describe(bankroll=1000, currency="S/ ")
    assert "Más de 0.5 goles @ 1.50" in text and "S/ " in text
    assert f"EN TU CASA: apuesta solo si paga {s.min_odds_display:.2f} o más" in text
    named = s.describe(house="Te Apuesto")
    assert "EN TE APUESTO: apuesta solo si paga" in named and "TU CASA" not in named


def test_min_odds_display_rounds_up_never_below_the_real_floor():
    s = find_signals(match(), ou(0.5, 1.50, 2.90), PROBS, ValueConfig(), NOW)[0]
    from dataclasses import replace
    for raw, shown in ((1.741, 1.75), (1.75, 1.75), (1.7501, 1.76), (1.50, 1.50)):
        assert replace(s, min_odds=raw).min_odds_display == pytest.approx(shown)
    assert s.min_odds_display >= s.min_odds and s.min_odds_display - s.min_odds < 0.01


def test_evaluate_quotes_explains_every_discard():
    from goalalert.value import evaluate_quotes, match_gate
    cfg, probs = ValueConfig(), PROBS
    reasons = lambda quotes, now=NOW: {(e.selection, e.line): e.reason for e in evaluate_quotes(match(), quotes, probs, cfg, now)}

    ok = reasons(ou(0.5, 1.50, 2.90))
    assert ok[(OVER, 0.5)] is None and "prob. combinada" in ok[(UNDER, 0.5)]
    assert "incompleto" in reasons(ou(0.5, 1.50, 2.90)[:1])[(OVER, 0.5)]
    assert reasons(ou(0.5, 1.50, 2.90, suspended=True))[(OVER, 0.5)] == "cuota suspendida"
    assert reasons(ou(0.5, 1.50, 2.90, updated_at=NOW - 600))[(OVER, 0.5)] == "cuota vieja: actualizada hace 600 s (límite 120 s)"
    assert "divergencia" in reasons(ou(0.5, 1.90, 1.90))[(OVER, 0.5)]
    assert "no soportada" in reasons(ou(1.0, 1.50, 2.90))[(OVER, 1.0)]
    assert "fuera de" in reasons(ou(0.5, 1.30, 3.60))[(OVER, 0.5)] or "EV" in reasons(ou(0.5, 1.30, 3.60))[(OVER, 0.5)]
    assert "minuto 3 fuera de la ventana 20-82" == match_gate(match(elapsed=3), cfg)
    assert match_gate(match(), cfg) is None
    assert "HT" in match_gate(match(status="HT", elapsed=45), cfg)


def test_find_signals_is_exactly_the_evaluations_without_a_reason():
    from goalalert.value import evaluate_quotes
    quotes = ou(0.5, 1.50, 2.90) + ou(1.5, 2.40, 1.55)
    cfg = ValueConfig(min_prob=0.0, min_ev=-1.0)  # nada se descarta por prob./EV
    passing = [(e.market, e.selection, e.line) for e in evaluate_quotes(match(), quotes, PROBS, cfg, NOW) if e.reason is None]
    signals = [(s.market, s.selection, s.line) for s in find_signals(match(), quotes, PROBS, cfg, NOW)]
    assert sorted(signals, key=str) == sorted(passing, key=str) and signals


def test_default_thresholds_can_fire_on_a_balanced_main_line():
    """Regresión: la API real ofrece UNA línea centrada (~50/50). Con min_prob=0.60 nada podía alertar ahí."""
    lam = -math.log(1 - 0.60)  # el modelo ve 60% de Over 0.5
    probs = MatchProbs(0, 0, lam / 2, lam / 2)
    sigs = find_signals(match(), ou(0.5, 1.95, 1.85), probs, ValueConfig(), NOW)
    assert [(s.selection, s.line) for s in sigs] == [(OVER, 0.5)]
    assert 0.55 <= sigs[0].p_final < 0.60 and sigs[0].ev >= 0.06


def test_quote_age_check_can_be_disabled():
    from dataclasses import replace
    old = ou(0.5, 1.50, 2.90, updated_at=NOW - 600)
    assert find_signals(match(), old, PROBS, ValueConfig(), NOW) == []
    assert len(find_signals(match(), old, PROBS, replace(ValueConfig(), max_odds_age_s=0), NOW)) == 1

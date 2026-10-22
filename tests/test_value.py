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
    assert find_signals(match(elapsed=3), q, PROBS, cfg, NOW) == []
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

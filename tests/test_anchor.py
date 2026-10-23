import pytest

from goalalert.anchor import build_probs, devig, market_rates
from goalalert.model import GoalModel, MatchProbs, ModelParams
from goalalert.models import AWAY, HOME, NONE, OVER, UNDER, LiveMatch, Market, Quote, TeamLive


def match(hg=0, ag=0, status="1H", elapsed=30, home=None, away=None):
    return LiveMatch(1, 281, "Liga 1", "Peru", status, elapsed, home or TeamLive("A", 1, hg), away or TeamLive("B", 2, ag))


def quotes_from(lam_h, lam_a, hg=0, ag=0, margin=0.05, lines=(1.5, 2.5), next_goal=True):
    """Cuotas con margen proporcional generadas desde tasas verdaderas: lo que un mercado 'perfecto' pondría."""
    probs = MatchProbs(hg, ag, lam_h, lam_a)
    out = []

    def odds(p):
        return 1 / (p * (1 + margin))

    for line in lines:
        p_over = probs.p_over(line)
        if 0.02 < p_over < 0.98:
            out += [Quote(1, Market.OVER_UNDER, OVER, line, odds(p_over)), Quote(1, Market.OVER_UNDER, UNDER, line, odds(1 - p_over))]
    if next_goal:
        ng = probs.p_next_goal()
        out += [Quote(1, Market.NEXT_GOAL, k, None, odds(ng[k])) for k in (HOME, AWAY, NONE)]
    return out


@pytest.mark.parametrize("lam_h,lam_a,hg,ag", [(1.6, 0.65, 0, 0), (0.8, 0.9, 1, 0), (0.35, 0.3, 1, 1), (1.1, 1.4, 0, 2)])
def test_anchor_recovers_the_true_rates_through_a_5_percent_margin(lam_h, lam_a, hg, ag):
    rates = market_rates(match(hg, ag), quotes_from(lam_h, lam_a, hg, ag), 0.56)
    assert rates.lam_home == pytest.approx(lam_h, abs=2e-3)
    assert rates.lam_away == pytest.approx(lam_a, abs=2e-3)


def test_anchor_uses_the_most_balanced_line_and_says_so():
    rates = market_rates(match(), quotes_from(1.6, 0.65), 0.56)
    assert "over/under 2.5" in rates.source  # P(over 2.5) ~ 0.45 está más cerca de 50% que P(over 1.5) ~ 0.8
    assert "reparto: próximo gol" in rates.source


def test_total_falls_back_to_next_goal_none_and_split_to_league_prior():
    only_next = quotes_from(1.2, 0.8, lines=(), next_goal=True)
    rates = market_rates(match(), only_next, 0.5)
    assert rates.lam_home + rates.lam_away == pytest.approx(2.0, abs=2e-3) and "ninguno" in rates.source
    only_ou = quotes_from(1.2, 0.8, next_goal=False)
    rates = market_rates(match(), only_ou, 0.60)
    assert rates.lam_home / (rates.lam_home + rates.lam_away) == pytest.approx(0.60)
    assert "prior de liga" in rates.source


def test_no_anchor_when_the_market_cannot_be_used():
    assert market_rates(match(), [], 0.5) is None
    suspended = [Quote(q.fixture_id, q.market, q.selection, q.line, q.odds, suspended=True) for q in quotes_from(1.2, 0.8)]
    assert market_rates(match(), suspended, 0.5) is None
    # línea ya superada (3 goles y la línea es 2.5): no informa de lo que falta
    assert market_rates(match(2, 1), quotes_from(1.0, 1.0, 0, 0, lines=(2.5,), next_goal=False), 0.5) is None


def test_devig_needs_a_complete_live_market():
    full = quotes_from(1.2, 0.8, lines=(1.5,), next_goal=False)
    p = devig(full)
    assert p[OVER] + p[UNDER] == pytest.approx(1.0)
    assert devig(full[:1]) is None and devig([]) is None


def test_model_with_anchor_equals_the_anchor_without_stats_and_moves_with_pressure():
    model = GoalModel()
    m = match()
    assert model.evaluate(m, (1.0, 0.8)).lam_home == pytest.approx(1.0)
    busy = match(home=TeamLive("A", 1, shots=14, shots_on_target=8), away=TeamLive("B", 2, shots=2, shots_on_target=0))
    probs = model.evaluate(busy, (1.0, 0.8))
    assert probs.lam_home > 1.0 and probs.lam_away < 0.8


def test_anchor_ignores_score_and_cards_because_the_market_already_prices_them():
    model = GoalModel()
    red = match(1, 0, home=TeamLive("A", 1, 1, red_cards=1))
    assert model.evaluate(red, (0.7, 0.9)).lam_home == pytest.approx(0.7)  # sin f_marcador ni f_rojas


def test_build_probs_respects_the_prior_setting():
    quotes = quotes_from(1.3, 0.9)
    probs, rates = build_probs(GoalModel(), match(), quotes)
    assert rates is not None and probs.lam_home == pytest.approx(rates.lam_home)
    probs, rates = build_probs(GoalModel(), match(), [])
    assert probs is None and rates is None  # prior=market y sin cuotas: no se inventa un modelo
    probs, rates = build_probs(GoalModel(ModelParams(prior="league")), match(), [])
    assert probs is not None and rates is None

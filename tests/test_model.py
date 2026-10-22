import math

import pytest

from goalalert.model import GoalModel, MatchProbs, ModelParams, poisson_sf, remaining_fraction
from goalalert.models import AWAY, HOME, NONE, LiveMatch, TeamLive


def match(status="2H", elapsed=60, hg=0, ag=0, hred=0, ared=0, h_sot=None, a_sot=None, league=1):
    return LiveMatch(
        1, league, "L", "C", status, elapsed,
        TeamLive("H", 1, hg, hred, shots=h_sot, shots_on_target=h_sot),
        TeamLive("A", 2, ag, ared, shots=a_sot, shots_on_target=a_sot),
    )


def test_remaining_fraction_profile():
    assert remaining_fraction("NS", 0) == 1.0
    assert remaining_fraction("HT", 45) == pytest.approx(0.55)
    assert remaining_fraction("FT", 90) == 0.0
    fracs = [remaining_fraction("1H", m) for m in range(0, 46)] + [remaining_fraction("2H", m) for m in range(46, 95)]
    assert all(a >= b for a, b in zip(fracs, fracs[1:]))
    assert fracs[-1] > 0  # en descuento todavía queda algo de tiempo


def test_poisson_sf():
    lam = 1.3
    assert poisson_sf(0, lam) == 1.0
    assert poisson_sf(1, lam) == pytest.approx(1 - math.exp(-lam))
    p2 = 1 - math.exp(-lam) - lam * math.exp(-lam)
    assert poisson_sf(2, lam) == pytest.approx(p2)
    assert poisson_sf(3, 0.0) == 0.0


def test_over_markets():
    pr = MatchProbs(2, 0, 0.6, 0.4)
    assert pr.p_over(1.5) == 1.0  # ya hay 2 goles
    assert pr.p_over(2.5) == pytest.approx(1 - math.exp(-1.0))
    assert pr.p_over(2.0) is None  # solo líneas .5


def test_btts():
    assert MatchProbs(1, 1, 0.5, 0.5).p_btts_yes() == 1.0
    assert MatchProbs(1, 0, 0.5, 0.8).p_btts_yes() == pytest.approx(1 - math.exp(-0.8))
    assert MatchProbs(0, 0, 0.5, 0.8).p_btts_yes() == pytest.approx((1 - math.exp(-0.5)) * (1 - math.exp(-0.8)))


def test_next_goal_is_a_distribution():
    ng = MatchProbs(0, 0, 0.9, 0.3).p_next_goal()
    assert sum(ng.values()) == pytest.approx(1.0)
    assert ng[HOME] > ng[AWAY] and ng[NONE] == pytest.approx(math.exp(-1.2))
    assert MatchProbs(0, 0, 0, 0).p_next_goal()[NONE] == 1.0


def test_goals_expected_fall_as_time_passes():
    model = GoalModel()
    early = model.evaluate(match("1H", 10)).lam_total
    late = model.evaluate(match("2H", 80)).lam_total
    assert early > late > 0


def test_trailing_team_pushes_harder():
    model = GoalModel()
    level = model.evaluate(match())
    home_down = model.evaluate(match(hg=0, ag=1))
    assert home_down.lam_home > level.lam_home
    assert home_down.lam_away < level.lam_away or home_down.lam_away == pytest.approx(level.lam_away * 0.92)


def test_red_card_shifts_rates():
    model = GoalModel()
    base = model.evaluate(match())
    red = model.evaluate(match(hred=1))
    assert red.lam_home < base.lam_home and red.lam_away > base.lam_away


def test_pressure_from_shots_raises_rate_and_is_bounded():
    model = GoalModel()
    base = model.evaluate(match("2H", 60))
    busy = model.evaluate(match("2H", 60, h_sot=9, a_sot=1))
    assert busy.lam_home > base.lam_home and busy.lam_away < base.lam_away
    absurd = model.evaluate(match("2H", 60, h_sot=60))
    ceiling = ModelParams().pressure_max
    no_press = model.evaluate(match("2H", 60)).lam_home
    assert absurd.lam_home == pytest.approx(no_press * ceiling)


def test_league_priors_override():
    params = ModelParams(league_priors={"281": (2.0, 1.0)})
    model = GoalModel(params)
    assert model.evaluate(match(league=281)).lam_home > model.evaluate(match(league=39)).lam_home

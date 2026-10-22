import pytest

from goalalert.models import AWAY, HOME, NONE, NO, OVER, UNDER, YES, Market
from goalalert.settle import LOST, VOID, WON, outcome_for
from goalalert.store import Store
from goalalert.value import Signal


def test_over_under_outcomes():
    assert outcome_for("over_under", OVER, 2.5, (0, 0), (2, 1)) == WON
    assert outcome_for("over_under", OVER, 2.5, (0, 0), (1, 1)) == LOST
    assert outcome_for("over_under", UNDER, 2.5, (0, 0), (1, 1)) == WON
    assert outcome_for("over_under", UNDER, 2.5, (0, 0), (3, 0)) == LOST


def test_btts_outcomes():
    assert outcome_for("btts", YES, None, (0, 0), (1, 2)) == WON
    assert outcome_for("btts", YES, None, (0, 0), (3, 0)) == LOST
    assert outcome_for("btts", NO, None, (0, 0), (3, 0)) == WON


def test_next_goal_outcomes():
    assert outcome_for("next_goal", HOME, None, (1, 1), (2, 1)) == WON
    assert outcome_for("next_goal", AWAY, None, (1, 1), (2, 1)) == LOST
    assert outcome_for("next_goal", NONE, None, (1, 1), (1, 1)) == WON
    assert outcome_for("next_goal", HOME, None, (1, 1), (1, 1)) == LOST
    # ambos anotaron después de la señal: el orden es desconocido => se anula
    assert outcome_for("next_goal", HOME, None, (0, 0), (1, 1)) == VOID


def sig(**kw):
    base = dict(
        fixture_id=7, league="Liga 1 (Peru)", home="A", away="B", minute=40, home_goals=0, away_goals=0,
        market=Market.OVER_UNDER, selection=OVER, line=1.5, odds=1.8, bookmaker="x",
        p_model=0.7, p_market=0.6, p_final=0.66, ev=0.19, stake_pct=0.01, lam_home=0.7, lam_away=0.6,
    )
    base.update(kw)
    return Signal(**base)


def test_store_dedupe_lookup_and_counts():
    st = Store(":memory:")
    s = sig()
    assert st.last_for_key(s) is None
    st.add(s, now=100.0)
    last = st.last_for_key(s)
    assert last["ev"] == pytest.approx(0.19) and last["created_at"] == 100.0
    assert st.last_for_key(sig(line=2.5)) is None
    assert st.last_for_key(sig(market=Market.BTTS, selection=YES, line=None)) is None
    assert st.count_for_fixture(7) == 1 and st.count_for_fixture(8) == 0


def test_store_settlement_and_report():
    st = Store(":memory:")
    a = st.add(sig(odds=2.0, p_final=0.6), now=1.0)
    b = st.add(sig(odds=2.0, p_final=0.6, selection=UNDER), now=2.0)
    st.add(sig(fixture_id=9), now=3.0)
    assert len(st.pending()) == 3
    st.settle(a, WON, now=10.0)
    st.settle(b, LOST, now=10.0)
    rep = st.report()
    assert rep["pending"] == 1 and rep["all"]["n"] == 2
    assert rep["all"]["hit_rate"] == 0.5
    assert rep["all"]["roi_flat"] == pytest.approx(0.0)  # +1 y -1
    assert rep["all"]["brier"] == pytest.approx(((0.6 - 1) ** 2 + (0.6 - 0) ** 2) / 2)
    assert rep["by_market"]["over_under"]["n"] == 2

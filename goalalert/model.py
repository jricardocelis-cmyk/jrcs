"""Modelo de goles en vivo.

Cada equipo anota goles restantes ~ Poisson(lambda_i), con

    lambda_i = base_i * fraccion_restante(t) * f_marcador * f_rojas * f_presion

- base_i: goles esperados a 90' (prior por liga, configurable).
- fraccion_restante: fracción de los goles de un partido que aún quedan por jugar,
  con perfil temporal (45% en el 1T, 55% en el 2T, incluyendo el descuento de cada tiempo).
- f_marcador: el que va perdiendo ataca más, el que gana se repliega.
- f_rojas: un expulsado anota menos y concede más.
- f_presion: razón bayesiana entre el xG observado y el xG esperado hasta ese minuto,
  encogida hacia 1 con `pressure_k0` goles de prior (poca evidencia => poco movimiento).

Todos los parámetros son supuestos razonables, no verdades: la forma de saber si
funcionan es el reporte de calibración sobre las señales registradas (`goalalert report`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models import AWAY, HOME, NONE, LiveMatch, TeamLive

FIRST_HALF_SHARE = 0.45
H1_LEN = 47.0  # 45' + ~2' de descuento
H2_LEN = 48.0  # 45' + ~3' de descuento
LIVE_STATUSES = ("1H", "2H")


def remaining_fraction(status: str, elapsed: int) -> float:
    """Fracción de los goles de un partido completo que se espera ver desde ahora."""
    if status in ("NS", "TBD"):
        return 1.0
    if status == "1H":
        left_1h = max(H1_LEN - elapsed, 1.0)
        return FIRST_HALF_SHARE * left_1h / H1_LEN + (1 - FIRST_HALF_SHARE)
    if status == "HT":
        return 1 - FIRST_HALF_SHARE
    if status == "2H":
        left_2h = max(H2_LEN - (elapsed - 45), 1.0)
        return (1 - FIRST_HALF_SHARE) * left_2h / H2_LEN
    return 0.0


def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k) para X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    term = math.exp(-lam)
    cdf = term
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def _default_score_mult() -> dict[int, float]:
    # Multiplicador de la intensidad propia según (goles propios - goles rivales).
    return {-2: 1.20, -1: 1.10, 0: 1.00, 1: 0.92, 2: 0.85}


@dataclass(frozen=True)
class ModelParams:
    home_xg: float = 1.45
    away_xg: float = 1.15
    league_priors: dict[str, tuple[float, float]] = field(default_factory=dict)
    red_own: float = 0.72
    red_opp: float = 1.25
    score_mult: dict[int, float] = field(default_factory=_default_score_mult)
    pressure_k0: float = 2.0
    pressure_min: float = 0.65
    pressure_max: float = 1.60
    xg_per_sot: float = 0.24
    xg_per_other_shot: float = 0.03
    # "market": parte de los goles esperados implícitos en las cuotas (conoce a los equipos); "league": prior de liga
    prior: str = "market"

    def priors_for(self, league_id: int) -> tuple[float, float]:
        return self.league_priors.get(str(league_id), (self.home_xg, self.away_xg))


class MatchProbs:
    """Probabilidades de los mercados de goles dado el estado actual y las tasas restantes."""

    def __init__(self, home_goals: int, away_goals: int, lam_home: float, lam_away: float):
        self.home_goals = home_goals
        self.away_goals = away_goals
        self.lam_home = lam_home
        self.lam_away = lam_away

    @property
    def lam_total(self) -> float:
        return self.lam_home + self.lam_away

    def p_over(self, line: float) -> float | None:
        """P(goles totales finales > line). Solo líneas .5 (sin push)."""
        if abs(line % 1 - 0.5) > 1e-9:
            return None
        needed = int(math.floor(line)) + 1 - (self.home_goals + self.away_goals)
        return poisson_sf(needed, self.lam_total)

    def p_btts_yes(self) -> float:
        p_home = 1.0 if self.home_goals >= 1 else 1 - math.exp(-self.lam_home)
        p_away = 1.0 if self.away_goals >= 1 else 1 - math.exp(-self.lam_away)
        return p_home * p_away

    def p_next_goal(self) -> dict[str, float]:
        lam = self.lam_total
        if lam <= 0:
            return {HOME: 0.0, AWAY: 0.0, NONE: 1.0}
        p_any = 1 - math.exp(-lam)
        return {
            HOME: p_any * self.lam_home / lam,
            AWAY: p_any * self.lam_away / lam,
            NONE: 1 - p_any,
        }


class GoalModel:
    def __init__(self, params: ModelParams | None = None):
        self.params = params or ModelParams()

    def _observed_xg(self, t: TeamLive) -> float | None:
        if t.xg is not None:
            return t.xg
        if t.shots_on_target is None and t.shots is None:
            return None
        sot = t.shots_on_target or 0
        other = max((t.shots if t.shots is not None else sot) - sot, 0)
        return self.params.xg_per_sot * sot + self.params.xg_per_other_shot * other

    def _pressure(self, t: TeamLive, expected_so_far: float) -> float:
        p = self.params
        obs = self._observed_xg(t)
        if obs is None:
            return 1.0
        ratio = (p.pressure_k0 + obs) / (p.pressure_k0 + expected_so_far)
        return min(max(ratio, p.pressure_min), p.pressure_max)

    def _score_mult(self, diff: int) -> float:
        keys = self.params.score_mult
        return keys[max(min(diff, max(keys)), min(keys))]

    def _red_mult(self, own: int, opp: int) -> float:
        p = self.params
        return (p.red_own ** min(own, 2)) * (p.red_opp ** min(opp, 2))

    def evaluate(self, m: LiveMatch, anchor: tuple[float, float] | None = None) -> MatchProbs:
        """`anchor` = goles restantes que el mercado espera (local, visita). Con ancla, el mercado ya pone precio a
        equipos, marcador y tarjetas: solo se ajusta por presión observada."""
        rem = remaining_fraction(m.status, m.elapsed)
        done = 1 - rem
        if anchor is not None:
            so_far = done / rem if rem > 0 else 0.0  # goles ya esperados hasta ahora, a la misma tasa que la restante
            return MatchProbs(
                m.home.goals, m.away.goals,
                anchor[0] * self._pressure(m.home, anchor[0] * so_far),
                anchor[1] * self._pressure(m.away, anchor[1] * so_far),
            )
        base_h, base_a = self.params.priors_for(m.league_id)
        lam_h = (
            base_h * rem
            * self._score_mult(m.home.goals - m.away.goals)
            * self._red_mult(m.home.red_cards, m.away.red_cards)
            * self._pressure(m.home, base_h * done)
        )
        lam_a = (
            base_a * rem
            * self._score_mult(m.away.goals - m.home.goals)
            * self._red_mult(m.away.red_cards, m.home.red_cards)
            * self._pressure(m.away, base_a * done)
        )
        return MatchProbs(m.home.goals, m.away.goals, lam_h, lam_a)

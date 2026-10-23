"""Ancla de mercado: goles esperados implícitos en las cuotas en vivo.

El mercado ya conoce la calidad de cada equipo, el marcador y las tarjetas; un prior de liga no. Aquí se
invierten las cuotas para obtener los goles restantes que el mercado espera de cada equipo:

- total: de la línea Over/Under más equilibrada, resolviendo P(Poisson(lambda) >= goles que faltan) = p_over
  (probabilidad sin margen). Si no hay Over/Under, de «ningún gol más» del mercado de próximo gol.
- reparto local/visita: del mercado de próximo gol, donde p_local / p_visita = lambda_local / lambda_visita.

El modelo parte de ahí y solo lo ajusta con la presión observada (tiros/xG): una alerta significa «el partido
se juega distinto a como lo cotiza el mercado», no «mi prior no conoce a estos equipos».
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .model import GoalModel, MatchProbs, poisson_sf
from .models import COMPLETE_SETS, HOME, AWAY, NONE, OVER, LiveMatch, Market, Quote


@dataclass(frozen=True)
class MarketRates:
    lam_home: float
    lam_away: float
    source: str  # de dónde salió, para el diagnóstico


def devig(group: list[Quote]) -> dict[str, float] | None:
    """Probabilidades sin margen (reparto proporcional) de un mercado completo y vigente; None si no sirve."""
    if not group or any(q.suspended or q.odds <= 1.0 for q in group):
        return None
    by_sel = {q.selection: q for q in group}
    if len(by_sel) != len(group) or set(by_sel) != COMPLETE_SETS[group[0].market]:
        return None
    total = sum(1 / q.odds for q in group)
    return {sel: (1 / q.odds) / total for sel, q in by_sel.items()}


def _solve_total_rate(needed: int, p_over: float) -> float:
    lo, hi = 1e-6, 15.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if poisson_sf(needed, mid) < p_over:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def market_rates(match: LiveMatch, quotes: list[Quote], default_home_share: float) -> MarketRates | None:
    goals_now = match.home.goals + match.away.goals

    best: tuple[float, float, float] | None = None  # (distancia a 50%, lambda total, línea)
    by_line: dict[tuple, list[Quote]] = {}
    for q in quotes:
        if q.market is Market.OVER_UNDER and q.line is not None:
            by_line.setdefault((q.line, q.bookmaker), []).append(q)
    for (line, _book), group in by_line.items():
        probs = devig(group)
        needed = int(math.floor(line)) + 1 - goals_now
        if probs is None or abs(line % 1 - 0.5) > 1e-9 or needed <= 0 or not 0.02 < probs[OVER] < 0.98:
            continue
        cand = (abs(probs[OVER] - 0.5), _solve_total_rate(needed, probs[OVER]), line)
        if best is None or cand[0] < best[0]:
            best = cand

    next_goal = devig([q for q in quotes if q.market is Market.NEXT_GOAL])

    if best is not None:
        lam_total, total_src = best[1], f"over/under {best[2]:g}"
    elif next_goal is not None and next_goal[NONE] > 0.01:
        lam_total, total_src = -math.log(next_goal[NONE]), "próximo gol (ninguno)"
    else:
        return None

    if next_goal is not None and next_goal[HOME] + next_goal[AWAY] > 0:
        share = next_goal[HOME] / (next_goal[HOME] + next_goal[AWAY])
        split_src = "próximo gol"
    else:
        share, split_src = default_home_share, "prior de liga"
    return MarketRates(lam_total * share, lam_total * (1 - share), f"total: {total_src} · reparto: {split_src}")


def build_probs(model: GoalModel, match: LiveMatch, quotes: list[Quote]) -> tuple[MatchProbs | None, MarketRates | None]:
    """Probabilidades del modelo según `prior`. (None, None) si falta el ancla de mercado y es obligatoria."""
    if model.params.prior != "market":
        return model.evaluate(match), None
    base_home, base_away = model.params.priors_for(match.league_id)
    rates = market_rates(match, quotes, base_home / (base_home + base_away))
    if rates is None:
        return None, None
    return model.evaluate(match, (rates.lam_home, rates.lam_away)), rates

"""Simulador de partidos en vivo para probar el flujo completo sin API key.

Cada partido tiene tasas de gol "verdaderas" ocultas. Las cuotas salen de esas tasas con margen
de la casa, ruido y, a veces, un rezago de un tick (cuota vieja tras un gol). Sirve para validar la
tubería (alertas, deduplicación, confirmación previa, liquidación), NO como evidencia de ventaja.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace

from ..model import H1_LEN, H2_LEN, MatchProbs, remaining_fraction
from ..models import (
    AWAY, HOME, NONE, NO, OVER, UNDER, YES,
    FinalResult, LiveMatch, Market, Quote, TeamLive,
)

FIXTURES = [
    ((281, "Liga 1", "Peru"), ("Alianza Lima", "Sporting Cristal")),
    ((281, "Liga 1", "Peru"), ("Universitario", "Melgar")),
    ((281, "Liga 1", "Peru"), ("Cienciano", "Sport Boys")),
    ((39, "Premier League", "England"), ("Arsenal", "Chelsea")),
    ((140, "La Liga", "Spain"), ("Real Madrid", "Sevilla")),
    ((135, "Serie A", "Italy"), ("Inter", "Napoli")),
    ((128, "Liga Profesional", "Argentina"), ("Boca Juniors", "River Plate")),
    ((71, "Serie A", "Brazil"), ("Flamengo", "Palmeiras")),
]


@dataclass
class _Sim:
    fixture_id: int
    league: tuple[int, str, str]
    names: tuple[str, str]
    lam: tuple[float, float]  # goles a 90' "verdaderos"
    clock: float = 0.0
    goals: tuple[int, int] = (0, 0)
    reds: tuple[int, int] = (0, 0)
    sot: tuple[int, int] = (0, 0)
    other: tuple[int, int] = (0, 0)
    prev_goals: tuple[int, int] = (0, 0)

    @property
    def status(self) -> str:
        if self.clock < H1_LEN:
            return "1H"
        if self.clock < H1_LEN + 1:
            return "HT"
        return "2H" if self.clock < H1_LEN + 1 + H2_LEN else "FT"

    @property
    def elapsed(self) -> int:
        if self.status == "1H":
            return min(int(self.clock), 45)
        if self.status == "HT":
            return 45
        return 46 + int(self.clock - H1_LEN - 1)


class SimProvider:
    def __init__(self, n_matches: int = 8, seed: int = 7, minutes_per_tick: float = 1.0,
                 margin: float = 0.05, market_noise: float = 0.10, lag_prob: float = 0.15):
        self.rng = random.Random(seed)
        self.minutes_per_tick = minutes_per_tick
        self.margin, self.market_noise, self.lag_prob = margin, market_noise, lag_prob
        self.sims: dict[int, _Sim] = {}
        for i in range(n_matches):
            lam = (1.45 * self.rng.lognormvariate(0, 0.30), 1.15 * self.rng.lognormvariate(0, 0.30))
            league, names = FIXTURES[i % len(FIXTURES)]
            self.sims[1000 + i] = _Sim(1000 + i, league, names, lam)

    # ---- avance del tiempo --------------------------------------------------------------
    def tick(self) -> None:
        for s in self.sims.values():
            if s.status == "FT":
                continue
            s.prev_goals = s.goals
            before = remaining_fraction(s.status, s.elapsed)
            s.clock += self.minutes_per_tick
            if s.status == "FT":
                after = 0.0
            else:
                after = remaining_fraction(s.status, s.elapsed)
            frac = max(before - after, 0.0)
            xg = []
            for i in (0, 1):
                red_mult = (0.72 ** s.reds[i]) * (1.25 ** s.reds[1 - i])
                xg.append(s.lam[i] * frac * red_mult)
            g = [self._poisson(x) for x in xg]
            s.goals = (s.goals[0] + g[0], s.goals[1] + g[1])
            s.sot = tuple(s.sot[i] + self._poisson(xg[i] / 0.24) for i in (0, 1))
            s.other = tuple(s.other[i] + self._poisson(xg[i] * 6.3) for i in (0, 1))
            if self.rng.random() < 0.004 * self.minutes_per_tick:
                who = self.rng.randrange(2)
                s.reds = (s.reds[0] + (who == 0), s.reds[1] + (who == 1))

    def _poisson(self, lam: float) -> int:
        limit, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= limit:
                return k
            k += 1

    # ---- interfaz Provider ---------------------------------------------------------------
    def _match(self, s: _Sim) -> LiveMatch:
        return LiveMatch(
            fixture_id=s.fixture_id, league_id=s.league[0], league_name=s.league[1], country=s.league[2],
            status=s.status, elapsed=s.elapsed,
            home=TeamLive(s.names[0], s.fixture_id * 10 + 1, s.goals[0], s.reds[0]),
            away=TeamLive(s.names[1], s.fixture_id * 10 + 2, s.goals[1], s.reds[1]),
        )

    def live_matches(self) -> list[LiveMatch]:
        return [self._match(s) for s in self.sims.values() if s.status in ("1H", "2H")]

    def enrich(self, match: LiveMatch) -> LiveMatch:
        s = self.sims[match.fixture_id]
        return replace(
            match,
            home=replace(match.home, shots=s.sot[0] + s.other[0], shots_on_target=s.sot[0]),
            away=replace(match.away, shots=s.sot[1] + s.other[1], shots_on_target=s.sot[1]),
        )

    def _quotes(self, s: _Sim, lagged: bool) -> list[Quote]:
        goals = s.prev_goals if lagged else s.goals
        rem = remaining_fraction(s.status, s.elapsed)
        noise = self.rng.lognormvariate(0, self.market_noise)
        lam = [
            s.lam[i] * rem * noise * (0.72 ** s.reds[i]) * (1.25 ** s.reds[1 - i]) for i in (0, 1)
        ]
        probs = MatchProbs(goals[0], goals[1], lam[0], lam[1])
        fid = s.fixture_id
        susp = self.rng.random() < 0.05

        def mk(market, sel, line, p):
            p = min(max(p, 0.001), 0.999)
            return Quote(fid, market, sel, line, round(1 / (p * (1 + self.margin)), 2), susp, "sim")

        out: list[Quote] = []
        for line in (0.5, 1.5, 2.5, 3.5, 4.5):
            p_over = probs.p_over(line)
            if 0.04 < p_over < 0.96:
                out += [mk(Market.OVER_UNDER, OVER, line, p_over), mk(Market.OVER_UNDER, UNDER, line, 1 - p_over)]
        p_yes = probs.p_btts_yes()
        out += [mk(Market.BTTS, YES, None, p_yes), mk(Market.BTTS, NO, None, 1 - p_yes)]
        ng = probs.p_next_goal()
        out += [mk(Market.NEXT_GOAL, k, None, ng[k]) for k in (HOME, AWAY, NONE)]
        return out

    def live_odds(self) -> dict[int, list[Quote]]:
        return {
            s.fixture_id: self._quotes(s, lagged=self.rng.random() < self.lag_prob)
            for s in self.sims.values() if s.status in ("1H", "2H")
        }

    def refresh(self, fixture_id: int) -> tuple[LiveMatch | None, list[Quote]]:
        s = self.sims[fixture_id]
        if s.status not in ("1H", "2H"):
            return None, []
        return self._match(s), self._quotes(s, lagged=False)

    def result(self, fixture_id: int) -> FinalResult | None:
        s = self.sims[fixture_id]
        if s.status != "FT":
            return None
        return FinalResult(fixture_id, s.goals[0], s.goals[1], finished=True)

    def all_finished(self) -> bool:
        return all(s.status == "FT" for s in self.sims.values())

"""Convierte probabilidades del modelo + cuotas en vivo en señales con valor esperado."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from .model import LIVE_STATUSES, MatchProbs
from .models import (
    AWAY, COMPLETE_SETS, HOME, NONE, OVER, YES,
    LiveMatch, Market, Quote,
)


@dataclass(frozen=True)
class ValueConfig:
    markets: tuple[str, ...] = ("over_under", "btts", "next_goal")
    min_ev: float = 0.06  # EV mínimo con la probabilidad combinada
    min_prob: float = 0.60  # probabilidad combinada mínima de acertar
    min_odds: float = 1.40
    max_odds: float = 4.00
    min_minute: int = 20  # antes de ~20' el modelo es casi solo el prior de liga: el mercado lo conoce mejor
    max_minute: int = 82
    market_weight: float = 0.40  # peso del mercado (sin margen) en la probabilidad final
    max_divergence: float = 0.15  # |p_modelo - p_mercado| mayor => dato viejo o modelo roto
    max_odds_age_s: float = 60.0
    kelly_fraction: float = 0.10
    max_stake_pct: float = 0.015  # tope de stake como fracción del bankroll


@dataclass(frozen=True)
class Signal:
    fixture_id: int
    league: str
    home: str
    away: str
    minute: int
    home_goals: int
    away_goals: int
    market: Market
    selection: str
    line: float | None
    odds: float
    bookmaker: str
    p_model: float
    p_market: float
    p_final: float
    ev: float
    stake_pct: float
    lam_home: float
    lam_away: float
    min_odds: float = 0.0  # cuota mínima (en cualquier casa) que mantiene el EV mínimo con p_final

    @property
    def key(self) -> tuple[int, str, str, float | None]:
        return (self.fixture_id, self.market.value, self.selection, self.line)

    def label(self) -> str:
        if self.market is Market.OVER_UNDER:
            word = "Más de" if self.selection == OVER else "Menos de"
            return f"{word} {self.line:g} goles"
        if self.market is Market.BTTS:
            return f"Ambos anotan: {'Sí' if self.selection == YES else 'No'}"
        who = {HOME: self.home, AWAY: self.away, NONE: "Ninguno"}[self.selection]
        return f"Próximo gol: {who}"

    @property
    def min_odds_display(self) -> float:
        """Cuota mínima redondeada hacia arriba a 2 decimales: «paga X o más» nunca debe quedar por debajo."""
        return math.ceil(self.min_odds * 100 - 1e-9) / 100

    def describe(self, bankroll: float = 0.0, currency: str = "", house: str = "") -> str:
        stake = f"{self.stake_pct * 100:.1f}% del bankroll"
        if bankroll > 0:
            stake += f" (≈ {currency}{bankroll * self.stake_pct:,.2f})"
        floor = ""
        if self.min_odds > 0:
            floor = (
                f"EN {house.upper() if house else 'TU CASA'}: apuesta solo si paga "
                f"{self.min_odds_display:.2f} o más en el mismo mercado y con el mismo marcador "
                f"(equilibrio {1 / self.p_final:.2f}); si paga menos, descarta.\n"
            )
        return (
            f"SEÑAL EN VIVO — {self.league}\n"
            f"{self.home} {self.home_goals}-{self.away_goals} {self.away} · min {self.minute}'\n"
            f"Apuesta: {self.label()} @ {self.odds:.2f}"
            f"{' (ref. ' + self.bookmaker + ')' if self.bookmaker else ''}\n"
            f"{floor}"
            f"Prob. modelo {self.p_model:.1%} · mercado {self.p_market:.1%} · "
            f"combinada {self.p_final:.1%}\n"
            f"Valor esperado {self.ev:+.1%} (a la cuota de referencia) · stake sugerido {stake}\n"
            f"Goles esperados restantes: {self.lam_home + self.lam_away:.2f} "
            f"(local {self.lam_home:.2f} / visita {self.lam_away:.2f})"
        )


def model_prob(probs: MatchProbs, market: Market, selection: str, line: float | None) -> float | None:
    if market is Market.OVER_UNDER:
        if line is None:
            return None
        p_over = probs.p_over(line)
        if p_over is None:
            return None
        return p_over if selection == OVER else 1 - p_over
    if market is Market.BTTS:
        p_yes = probs.p_btts_yes()
        return p_yes if selection == YES else 1 - p_yes
    if market is Market.NEXT_GOAL:
        return probs.p_next_goal().get(selection)
    return None


def kelly_stake(p: float, odds: float, fraction: float, cap: float) -> float:
    full = (p * odds - 1) / (odds - 1)
    return max(0.0, min(full * fraction, cap))


def find_signals(
    match: LiveMatch,
    quotes: list[Quote],
    probs: MatchProbs,
    cfg: ValueConfig,
    now: float,
) -> list[Signal]:
    if match.status not in LIVE_STATUSES or not (cfg.min_minute <= match.elapsed <= cfg.max_minute):
        return []

    groups: dict[tuple, list[Quote]] = defaultdict(list)
    for q in quotes:
        if q.market.value in cfg.markets:
            groups[(q.market, q.line, q.bookmaker)].append(q)

    out: list[Signal] = []
    for (market, line, _book), group in groups.items():
        by_sel = {q.selection: q for q in group}
        if set(by_sel) != COMPLETE_SETS[market] or len(group) != len(by_sel):
            continue  # sin mercado completo no se puede quitar el margen ni validar
        if any(q.suspended or q.odds <= 1.0 for q in group):
            continue
        if any(q.updated_at is not None and now - q.updated_at > cfg.max_odds_age_s for q in group):
            continue

        overround = sum(1 / q.odds for q in group)
        for sel, q in by_sel.items():
            p_model = model_prob(probs, market, sel, line)
            if p_model is None:
                continue
            p_market = (1 / q.odds) / overround
            if abs(p_model - p_market) > cfg.max_divergence:
                continue
            p_final = (1 - cfg.market_weight) * p_model + cfg.market_weight * p_market
            ev = p_final * q.odds - 1
            if not (cfg.min_odds <= q.odds <= cfg.max_odds):
                continue
            if p_final < cfg.min_prob or ev < cfg.min_ev:
                continue
            out.append(Signal(
                fixture_id=match.fixture_id,
                league=f"{match.league_name} ({match.country})" if match.country else match.league_name,
                home=match.home.name, away=match.away.name,
                minute=match.elapsed,
                home_goals=match.home.goals, away_goals=match.away.goals,
                market=market, selection=sel, line=line,
                odds=q.odds, bookmaker=q.bookmaker,
                p_model=p_model, p_market=p_market, p_final=p_final, ev=ev,
                stake_pct=kelly_stake(p_final, q.odds, cfg.kelly_fraction, cfg.max_stake_pct),
                lam_home=probs.lam_home, lam_away=probs.lam_away,
                min_odds=(1 + cfg.min_ev) / p_final,
            ))
    out.sort(key=lambda s: s.ev, reverse=True)
    return out

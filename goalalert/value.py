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
    # Las líneas en vivo reales vienen centradas (~50%). Con min_prob=0.60, market_weight=0.40 y
    # max_divergence=0.15 una línea 50/50 nunca puede alertar (haría falta un 67% del modelo = 17 pp de brecha).
    min_prob: float = 0.55  # probabilidad combinada mínima de acertar
    min_odds: float = 1.40
    max_odds: float = 4.00
    min_minute: int = 20  # antes de ~20' el modelo es casi solo el prior de liga: el mercado lo conoce mejor
    max_minute: int = 82
    market_weight: float = 0.40  # peso del mercado (sin margen) en la probabilidad final
    max_divergence: float = 0.15  # |p_modelo - p_mercado| mayor => dato viejo o modelo roto
    max_odds_age_s: float = 120.0  # 0 desactiva el chequeo (el reconfirmado previo a cada alerta sigue activo)
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
        return selection_label(self.market, self.selection, self.line, self.home, self.away)

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


def selection_label(market: Market, selection: str, line: float | None, home: str, away: str) -> str:
    if market is Market.OVER_UNDER:
        word = "Más de" if selection == OVER else "Menos de"
        return f"{word} {line:g} goles"
    if market is Market.BTTS:
        return f"Ambos anotan: {'Sí' if selection == YES else 'No'}"
    who = {HOME: home, AWAY: away, NONE: "Ninguno"}[selection]
    return f"Próximo gol: {who}"


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


@dataclass(frozen=True)
class Evaluation:
    """Una apuesta evaluada. `reason` es None si pasa todos los filtros; si no, por qué se descarta."""
    market: Market
    selection: str
    line: float | None
    odds: float
    bookmaker: str
    p_model: float | None = None
    p_market: float | None = None
    p_final: float | None = None
    ev: float | None = None
    reason: str | None = None


def match_gate(match: LiveMatch, cfg: ValueConfig) -> str | None:
    """Motivo por el que el partido no se evalúa ahora, o None si está dentro de la ventana."""
    if match.status not in LIVE_STATUSES:
        return f"estado {match.status or '?'}: solo se evalúan 1H y 2H"
    if not (cfg.min_minute <= match.elapsed <= cfg.max_minute):
        return f"minuto {match.elapsed} fuera de la ventana {cfg.min_minute}-{cfg.max_minute}"
    return None


def evaluate_quotes(
    match: LiveMatch,
    quotes: list[Quote],
    probs: MatchProbs,
    cfg: ValueConfig,
    now: float,
) -> list[Evaluation]:
    """Aplica los filtros de valor a cada cuota y devuelve todas, con su motivo de descarte si lo hay."""
    groups: dict[tuple, list[Quote]] = defaultdict(list)
    for q in quotes:
        if q.market.value in cfg.markets:
            groups[(q.market, q.line, q.bookmaker)].append(q)

    out: list[Evaluation] = []
    for (market, line, book), group in groups.items():
        def reject(reason: str) -> None:
            out.extend(Evaluation(market, q.selection, line, q.odds, book, reason=reason) for q in group)

        by_sel = {q.selection: q for q in group}
        missing = COMPLETE_SETS[market] - set(by_sel)
        if missing or len(group) != len(by_sel):
            detail = f" (faltan: {', '.join(sorted(missing))})" if missing else ""
            reject(f"mercado incompleto o duplicado{detail}: no se puede quitar el margen ni validar")
            continue
        if any(q.suspended or q.odds <= 1.0 for q in group):
            reject("cuota suspendida")
            continue
        if cfg.max_odds_age_s > 0:
            ages = [now - q.updated_at for q in group if q.updated_at is not None]
            if ages and max(ages) > cfg.max_odds_age_s:
                reject(f"cuota vieja: actualizada hace {max(ages):.0f} s (límite {cfg.max_odds_age_s:.0f} s)")
                continue

        overround = sum(1 / q.odds for q in group)
        for sel, q in by_sel.items():
            base = dict(market=market, selection=sel, line=line, odds=q.odds, bookmaker=book)
            p_model = model_prob(probs, market, sel, line)
            if p_model is None:
                out.append(Evaluation(**base, reason="línea no soportada (solo líneas .5)"))
                continue
            p_market = (1 / q.odds) / overround
            gap = abs(p_model - p_market)
            if gap > cfg.max_divergence:
                out.append(Evaluation(
                    **base, p_model=p_model, p_market=p_market,
                    reason=f"divergencia {gap * 100:.0f} pp > {cfg.max_divergence * 100:.0f} pp (dato viejo o modelo roto)"))
                continue
            p_final = (1 - cfg.market_weight) * p_model + cfg.market_weight * p_market
            ev = p_final * q.odds - 1
            if not (cfg.min_odds <= q.odds <= cfg.max_odds):
                reason = f"cuota {q.odds:.2f} fuera de {cfg.min_odds:.2f}-{cfg.max_odds:.2f}"
            elif p_final < cfg.min_prob:
                reason = f"prob. combinada {p_final:.0%} < {cfg.min_prob:.0%}"
            elif ev < cfg.min_ev:
                reason = f"EV {ev:+.1%} < {cfg.min_ev:.0%}"
            else:
                reason = None
            out.append(Evaluation(**base, p_model=p_model, p_market=p_market, p_final=p_final, ev=ev, reason=reason))
    return out


def find_signals(
    match: LiveMatch,
    quotes: list[Quote],
    probs: MatchProbs,
    cfg: ValueConfig,
    now: float,
) -> list[Signal]:
    if match_gate(match, cfg) is not None:
        return []
    out: list[Signal] = []
    for e in evaluate_quotes(match, quotes, probs, cfg, now):
        if e.reason is not None:
            continue
        out.append(Signal(
            fixture_id=match.fixture_id,
            league=f"{match.league_name} ({match.country})" if match.country else match.league_name,
            home=match.home.name, away=match.away.name,
            minute=match.elapsed,
            home_goals=match.home.goals, away_goals=match.away.goals,
            market=e.market, selection=e.selection, line=e.line,
            odds=e.odds, bookmaker=e.bookmaker,
            p_model=e.p_model, p_market=e.p_market, p_final=e.p_final, ev=e.ev,
            stake_pct=kelly_stake(e.p_final, e.odds, cfg.kelly_fraction, cfg.max_stake_pct),
            lam_home=probs.lam_home, lam_away=probs.lam_away,
            min_odds=(1 + cfg.min_ev) / e.p_final,
        ))
    out.sort(key=lambda sig: sig.ev, reverse=True)
    return out

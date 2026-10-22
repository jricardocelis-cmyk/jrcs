"""Tipos compartidos entre proveedores, modelo, motor de valor y notificadores."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Market(str, Enum):
    OVER_UNDER = "over_under"
    BTTS = "btts"
    NEXT_GOAL = "next_goal"


OVER, UNDER = "over", "under"
YES, NO = "yes", "no"
HOME, AWAY, NONE = "home", "away", "none"

# Selecciones que forman un mercado completo (necesarias para quitar el margen de la casa).
COMPLETE_SETS: dict[Market, frozenset[str]] = {
    Market.OVER_UNDER: frozenset({OVER, UNDER}),
    Market.BTTS: frozenset({YES, NO}),
    Market.NEXT_GOAL: frozenset({HOME, AWAY, NONE}),
}


@dataclass(frozen=True)
class TeamLive:
    name: str
    team_id: int = 0
    goals: int = 0
    red_cards: int = 0
    shots: int | None = None
    shots_on_target: int | None = None
    xg: float | None = None


@dataclass(frozen=True)
class LiveMatch:
    fixture_id: int
    league_id: int
    league_name: str
    country: str
    status: str  # "1H", "HT", "2H", ...
    elapsed: int
    home: TeamLive
    away: TeamLive
    red_known: bool = True  # False si el proveedor no entregó eventos (tarjetas)


@dataclass(frozen=True)
class Quote:
    fixture_id: int
    market: Market
    selection: str
    line: float | None  # línea de goles para OVER_UNDER; None en el resto
    odds: float
    suspended: bool = False
    bookmaker: str = ""
    updated_at: float | None = None  # epoch segundos, si el proveedor lo informa


@dataclass(frozen=True)
class FinalResult:
    fixture_id: int
    home_goals: int
    away_goals: int
    finished: bool
    voided: bool = False  # suspendido/cancelado/walkover: las apuestas se anulan

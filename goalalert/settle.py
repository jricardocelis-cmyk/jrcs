"""Liquidación de señales a partir del marcador final (90')."""
from __future__ import annotations

from .models import AWAY, HOME, NONE, OVER, YES, Market

WON, LOST, VOID = "won", "lost", "void"


def outcome_for(
    market: str,
    selection: str,
    line: float | None,
    signal_goals: tuple[int, int],
    final_goals: tuple[int, int],
) -> str:
    fh, fa = final_goals
    if market == Market.OVER_UNDER.value:
        if line is None:
            return VOID
        went_over = fh + fa > line
        return WON if went_over == (selection == OVER) else LOST
    if market == Market.BTTS.value:
        both = fh > 0 and fa > 0
        return WON if both == (selection == YES) else LOST
    if market == Market.NEXT_GOAL.value:
        dh, da = fh - signal_goals[0], fa - signal_goals[1]
        if dh > 0 and da > 0:
            return VOID  # no se puede saber quién anotó primero solo con el marcador final
        actual = HOME if dh > 0 else AWAY if da > 0 else NONE
        return WON if actual == selection else LOST
    return VOID

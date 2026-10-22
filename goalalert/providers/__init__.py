"""Proveedores de datos en vivo (marcador, estadísticas y cuotas)."""
from __future__ import annotations

from typing import Protocol

from ..models import FinalResult, LiveMatch, Quote


class ProviderError(RuntimeError):
    pass


class Provider(Protocol):
    def live_matches(self) -> list[LiveMatch]:
        """Partidos en juego ahora (ya filtrados por liga si corresponde)."""

    def live_odds(self) -> dict[int, list[Quote]]:
        """Cuotas en vivo por fixture_id."""

    def enrich(self, match: LiveMatch) -> LiveMatch:
        """Agrega tiros/xG/tarjetas si el partido es candidato (puede costar requests)."""

    def refresh(self, fixture_id: int) -> tuple[LiveMatch | None, list[Quote]]:
        """Re-consulta un partido y sus cuotas justo antes de alertar."""

    def result(self, fixture_id: int) -> FinalResult | None:
        """Marcador final a 90' (o None si el dato aún no está disponible)."""

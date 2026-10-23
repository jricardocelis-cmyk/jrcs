"""Competiciones que más ofrecen las casas de apuestas, por id de API-Football.

Los ids salen de la documentación de la API y NO se pudieron contrastar al escribirlos: `goalalert coverage`
los verifica contra tu cuenta (nombre real, temporada en curso y cobertura) y avisa si alguno no coincide.
"""
from __future__ import annotations

import unicodedata

# id -> (etiqueta, pista del nombre que la API devuelve para ese id, sin acentos y en minúsculas)
POPULAR: dict[int, tuple[str, str]] = {
    2: ("UEFA Champions League", "champions league"),
    3: ("UEFA Europa League", "europa league"),
    848: ("UEFA Conference League", "conference"),
    39: ("Premier League (Inglaterra)", "premier league"),
    140: ("La Liga (España)", "la liga"),
    135: ("Serie A (Italia)", "serie a"),
    78: ("Bundesliga (Alemania)", "bundesliga"),
    61: ("Ligue 1 (Francia)", "ligue 1"),
    94: ("Primeira Liga (Portugal)", "primeira"),
    88: ("Eredivisie (Países Bajos)", "eredivisie"),
    203: ("Süper Lig (Turquía)", "super lig"),
    40: ("Championship (Inglaterra)", "championship"),
    13: ("CONMEBOL Libertadores", "libertadores"),
    11: ("CONMEBOL Sudamericana", "sudamericana"),
    71: ("Brasileirão Serie A", "serie a"),
    128: ("Liga Profesional (Argentina)", "liga profesional"),
    262: ("Liga MX (México)", "liga mx"),
    253: ("MLS (EE. UU.)", "major league soccer"),
    281: ("Liga 1 (Perú)", "primera"),
    1: ("Mundial", "world cup"),
    4: ("Eurocopa", "euro"),
    9: ("Copa América", "copa america"),
    5: ("UEFA Nations League", "nations league"),
    10: ("Amistosos de selecciones", "friendlies"),
    34: ("Eliminatorias CONMEBOL", "south america"),
    45: ("FA Cup", "fa cup"),
    48: ("EFL Cup", "league cup"),
    143: ("Copa del Rey", "copa del rey"),
    137: ("Coppa Italia", "coppa italia"),
    81: ("DFB Pokal", "dfb pokal"),
    66: ("Coupe de France", "coupe de france"),
    73: ("Copa do Brasil", "copa do brasil"),
}


def fold(text: str | None) -> str:
    """Minúsculas y sin acentos, para comparar nombres de ligas."""
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)).lower()


def expand_leagues(items) -> tuple[int, ...]:
    """['popular', 999] -> ids de las populares + 999, sin duplicados y en orden."""
    if isinstance(items, str):
        items = [items]
    out: list[int] = []
    for item in items:
        out.extend(POPULAR if item == "popular" else [int(item)])
    return tuple(dict.fromkeys(out))


def name_matches(league_id: int, api_name: str | None) -> bool:
    """¿El nombre que devuelve la API para este id es el que se esperaba? (siempre True si no es una liga conocida)."""
    hint = POPULAR.get(league_id, ("", ""))[1]
    return not hint or hint in fold(api_name)

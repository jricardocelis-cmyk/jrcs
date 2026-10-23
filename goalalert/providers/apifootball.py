"""Proveedor API-Football v3 (api-sports.io): marcador, eventos, estadísticas y cuotas en vivo.

NOTA: los parsers siguen la forma documentada de la API, pero no se pudieron contrastar contra
respuestas reales al escribirlos. Los nombres de mercado varían, por eso son configurables
(`[provider.bet_names]`) y `goalalert markets` imprime los que devuelve tu cuenta.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..models import (
    AWAY, HOME, NONE, NO, OVER, UNDER, YES,
    FinalResult, LiveMatch, Market, Quote, TeamLive,
)
from . import ProviderError

log = logging.getLogger(__name__)

BASE_URL = "https://v3.football.api-sports.io"
VOID_STATUSES = {"PST", "CANC", "ABD", "AWD", "WO"}
FINISHED_STATUSES = {"FT", "AET", "PEN"}

# Solo nombres vistos en cuotas en vivo reales. «Match Goals» existe pero NO se mapea hasta confirmar qué
# son sus valores (`goalalert markets` los muestra): mezclarlo con Over/Under podría crear señales falsas.
DEFAULT_BET_NAMES: dict[str, tuple[str, ...]] = {
    "over_under": ("over/under line",),
    "btts": ("both teams to score",),
    "next_goal": ("next goal",),
}

# La API no tiene «próximo gol»: tiene «¿Qué equipo anotará el N-ésimo gol?». Equivale al próximo gol
# solo cuando N = goles actuales + 1; los demás N se descartan.
_NTH_GOAL = re.compile(r"^which team will score the (\d+)(?:st|nd|rd|th) goal\??$")
# Nombres que `markets` muestra con valores crudos para verificar el mapeo.
_SAMPLE_KEYS = ("over/under", "match goals", "both teams to score", "which team will score the", "next goal")

_HOME_WORDS = {"home", "1"}
_AWAY_WORDS = {"away", "2"}
_NONE_WORDS = {"no goal", "none", "no goals", "nobody"}


@dataclass
class ApiFootball:
    api_key: str
    league_ids: tuple[int, ...] = ()
    bet_names: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_BET_NAMES))
    stats_ttl_s: float = 120.0
    max_enrich_per_cycle: int = 15
    request_reserve: int = 50  # no gastar requests extra si quedan menos que esto en el día
    timeout_s: float = 15.0
    remaining_requests: int | None = None
    _stats_cache: dict = field(default_factory=dict)
    _enriched_this_cycle: int = 0
    odds_meta: dict = field(default_factory=dict)  # fixture_id -> {'update': texto crudo, 'status': ...}

    # ---- HTTP -------------------------------------------------------------------------
    def _get(self, path: str, **params) -> list:
        url = f"{BASE_URL}/{path}" + (f"?{urlencode(params)}" if params else "")
        req = Request(url, headers={"x-apisports-key": self.api_key, "Accept": "application/json"})
        try:
            with urlopen(req, timeout=self.timeout_s) as resp:
                remaining = resp.headers.get("x-ratelimit-requests-remaining")
                if remaining is not None and remaining.isdigit():
                    self.remaining_requests = int(remaining)
                data = json.load(resp)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ProviderError(f"{path}: {exc}") from exc
        errors = data.get("errors")
        if errors:
            raise ProviderError(f"{path}: {errors}")
        return data.get("response") or []

    def _budget_ok(self) -> bool:
        return self.remaining_requests is None or self.remaining_requests > self.request_reserve

    # ---- Provider interface -----------------------------------------------------------
    def live_matches(self) -> list[LiveMatch]:
        self._enriched_this_cycle = 0
        matches = [parse_live_fixture(it) for it in self._get("fixtures", live="all")]
        if self.league_ids:
            matches = [m for m in matches if m.league_id in self.league_ids]
        return matches

    def live_odds(self) -> dict[int, list[Quote]]:
        names = _reverse_bet_names(self.bet_names)
        out: dict[int, list[Quote]] = {}
        for item in self._get("odds/live"):
            self.odds_meta[item["fixture"]["id"]] = {"update": item.get("update"), "status": item.get("status")}
            quotes = parse_live_odds(item, names)
            if quotes:
                out.setdefault(quotes[0].fixture_id, []).extend(quotes)
        return out

    def enrich(self, match: LiveMatch) -> LiveMatch:
        cached = self._stats_cache.get(match.fixture_id)
        now = time.time()
        if cached and now - cached[0] < self.stats_ttl_s:
            return _apply_extra(match, *cached[1:])
        if (not self._budget_ok()) or self._enriched_this_cycle >= self.max_enrich_per_cycle:
            return _apply_extra(match, *(cached[1:] if cached else ({}, None)))
        self._enriched_this_cycle += 1
        try:
            stats = parse_statistics(self._get("fixtures/statistics", fixture=match.fixture_id), match)
            reds = None
            if not match.red_known:
                reds = count_red_cards(self._get("fixtures/events", fixture=match.fixture_id), match)
        except ProviderError as exc:
            log.warning("enrich %s falló: %s", match.fixture_id, exc)
            return _apply_extra(match, *(cached[1:] if cached else ({}, None)))
        self._stats_cache[match.fixture_id] = (now, stats, reds)
        return _apply_extra(match, stats, reds)

    def refresh(self, fixture_id: int) -> tuple[LiveMatch | None, list[Quote]]:
        fixtures = self._get("fixtures", id=fixture_id)
        match = parse_live_fixture(fixtures[0]) if fixtures else None
        names = _reverse_bet_names(self.bet_names)
        quotes: list[Quote] = []
        for item in self._get("odds/live", fixture=fixture_id):
            quotes.extend(parse_live_odds(item, names))
        return match, quotes

    def result(self, fixture_id: int) -> FinalResult | None:
        items = self._get("fixtures", id=fixture_id)
        if not items:
            return None
        return parse_result(items[0])

    def coverage(self, league_id: int) -> dict | None:
        """Cobertura declarada de la liga en su temporada actual (estadísticas, eventos, cuotas)."""
        items = self._get("leagues", id=league_id, current="true")
        if not items:
            return None
        league = items[0].get("league") or {}
        seasons = items[0].get("seasons") or []
        season = next((x for x in seasons if x.get("current")), seasons[-1] if seasons else {})
        return {"name": league.get("name"), "country": (items[0].get("country") or {}).get("name"),
                "season": season.get("year"), "coverage": season.get("coverage") or {}}

    def inspect(self) -> tuple[Counter, Counter, dict]:
        """Ligas en vivo, nombres de mercado y una muestra de valores crudos (para configurar y verificar el mapeo)."""
        leagues = Counter()
        for it in self._get("fixtures", live="all"):
            m = parse_live_fixture(it)
            leagues[(m.league_id, m.league_name, m.country)] += 1
        bets: Counter = Counter()
        samples: dict[str, dict] = {}
        for it in self._get("odds/live"):
            teams = it.get("teams") or {}
            score = f"{(teams.get('home') or {}).get('goals', '?')}-{(teams.get('away') or {}).get('goals', '?')}"
            for b in it.get("odds") or []:
                name = b.get("name") or "?"
                bets[name] += 1
                low = name.lower()
                if name not in samples and any(k in low for k in _SAMPLE_KEYS) and not any(
                        x in low for x in ("corner", "1st half", "2nd half")):
                    samples[name] = {"fixture": it["fixture"]["id"], "score": score,
                                     "values": (b.get("values") or [])[:8]}
        return leagues, bets, samples


# ---- Parsers (puros, testeables) -------------------------------------------------------
def _reverse_bet_names(bet_names: dict[str, tuple[str, ...]]) -> dict[str, Market]:
    return {n.strip().lower(): Market(m) for m, names in bet_names.items() for n in names}


def parse_live_fixture(item: dict) -> LiveMatch:
    fx, league, teams = item["fixture"], item["league"], item["teams"]
    goals = item.get("goals") or {}
    home_id, away_id = teams["home"]["id"], teams["away"]["id"]
    events = item.get("events")
    reds = _reds_by_team(events or [])
    status = fx.get("status") or {}
    return LiveMatch(
        fixture_id=fx["id"],
        league_id=league["id"],
        league_name=league.get("name", ""),
        country=league.get("country", "") or "",
        status=status.get("short") or "",
        elapsed=int(status.get("elapsed") or 0),
        home=TeamLive(teams["home"]["name"], home_id, int(goals.get("home") or 0), reds.get(home_id, 0)),
        away=TeamLive(teams["away"]["name"], away_id, int(goals.get("away") or 0), reds.get(away_id, 0)),
        red_known=events is not None,
    )


def _reds_by_team(events: list[dict]) -> dict[int, int]:
    reds: dict[int, int] = {}
    for e in events:
        detail = (e.get("detail") or "").lower()
        if e.get("type") == "Card" and ("red" in detail or "second yellow" in detail):
            tid = (e.get("team") or {}).get("id")
            reds[tid] = reds.get(tid, 0) + 1
    return reds


def count_red_cards(events: list[dict], match: LiveMatch) -> tuple[int, int]:
    reds = _reds_by_team(events)
    return reds.get(match.home.team_id, 0), reds.get(match.away.team_id, 0)


def _num(v) -> float | None:
    try:
        return float(str(v).rstrip("%"))
    except (TypeError, ValueError):
        return None


def parse_statistics(response: list[dict], match: LiveMatch) -> dict[int, dict[str, float | None]]:
    out: dict[int, dict[str, float | None]] = {}
    for block in response:
        tid = (block.get("team") or {}).get("id")
        vals = {s.get("type"): _num(s.get("value")) for s in block.get("statistics") or []}
        out[tid] = {
            "shots": vals.get("Total Shots"),
            "sot": vals.get("Shots on Goal"),
            "xg": vals.get("expected_goals"),
        }
    return out


def _apply_extra(match: LiveMatch, stats: dict, reds: tuple[int, int] | None) -> LiveMatch:
    def team(t: TeamLive, red: int | None) -> TeamLive:
        s = stats.get(t.team_id) or {}
        return replace(
            t,
            red_cards=t.red_cards if red is None else red,
            shots=None if s.get("shots") is None else int(s["shots"]),
            shots_on_target=None if s.get("sot") is None else int(s["sot"]),
            xg=s.get("xg"),
        )

    h_red, a_red = reds if reds is not None else (None, None)
    return replace(
        match,
        home=team(match.home, h_red),
        away=team(match.away, a_red),
        red_known=match.red_known or reds is not None,
    )


def parse_result(item: dict) -> FinalResult:
    fx_id = item["fixture"]["id"]
    short = (item["fixture"].get("status") or {}).get("short") or ""
    if short in VOID_STATUSES:
        return FinalResult(fx_id, 0, 0, finished=True, voided=True)
    if short not in FINISHED_STATUSES:
        return FinalResult(fx_id, 0, 0, finished=False)
    ft = (item.get("score") or {}).get("fulltime") or {}  # 90': excluye prórroga y penales
    goals = item.get("goals") or {}
    home = ft.get("home") if ft.get("home") is not None else goals.get("home")
    away = ft.get("away") if ft.get("away") is not None else goals.get("away")
    return FinalResult(fx_id, int(home or 0), int(away or 0), finished=True)


def _iso_to_epoch(value) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # la API trabaja en UTC; sin zona, .timestamp() asumiría la hora local del equipo
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _parse_line(handicap, value: str) -> float | None:
    for raw in (handicap, value):
        if raw is None:
            continue
        m = re.search(r"-?\d+(?:\.\d+)?", str(raw))
        if m:
            return float(m.group())
    return None


def parse_live_odds(item: dict, bet_names: dict[str, Market]) -> list[Quote]:
    fixture_id = item["fixture"]["id"]
    st = item.get("status") or {}
    halted = bool(st.get("stopped") or st.get("blocked") or st.get("finished"))
    teams = item.get("teams") or {}
    total_goals = int((teams.get("home") or {}).get("goals") or 0) + int((teams.get("away") or {}).get("goals") or 0)
    updated = _iso_to_epoch(item.get("update"))

    quotes: list[Quote] = []
    for bet in item.get("odds") or []:
        name = (bet.get("name") or "").strip().lower()
        market = bet_names.get(name)
        nth = _NTH_GOAL.match(name)
        if market is None and nth:
            if int(nth.group(1)) != total_goals + 1:
                continue  # gol 3 con 0-0 en juego: no es el próximo gol
            market = Market.NEXT_GOAL
        if market is None:
            continue
        for v in bet.get("values") or []:
            value = str(v.get("value") or "").strip().lower()
            try:
                odds = float(v.get("odd"))
            except (TypeError, ValueError):
                continue
            selection, line = None, None
            if market is Market.OVER_UNDER:
                selection = OVER if value.startswith("over") else UNDER if value.startswith("under") else None
                line = _parse_line(v.get("handicap"), value)
                if line is None or abs(line % 1 - 0.5) > 1e-9:
                    continue  # solo líneas .5 (sin push ni cuartos asiáticos)
            elif market is Market.BTTS:
                selection = YES if value == "yes" else NO if value == "no" else None
            elif market is Market.NEXT_GOAL:
                selection = HOME if value in _HOME_WORDS else AWAY if value in _AWAY_WORDS else NONE if value in _NONE_WORDS else None
                goal_no = v.get("handicap")
                if goal_no not in (None, "") and _num(goal_no) != total_goals + 1:
                    continue  # "gol N" distinto al próximo gol
            if selection is None:
                continue
            quotes.append(Quote(
                fixture_id=fixture_id, market=market, selection=selection, line=line, odds=odds,
                suspended=halted or bool(v.get("suspended")), bookmaker="api-football",
                updated_at=updated,
            ))
    return quotes

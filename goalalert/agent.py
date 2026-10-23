"""Bucle principal: leer partidos y cuotas, evaluar, confirmar, alertar y liquidar."""
from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Callable

from .anchor import build_probs
from .config import Config
from .model import LIVE_STATUSES, GoalModel
from .models import LiveMatch
from .notifiers import send_all
from .providers import ProviderError
from .settle import outcome_for
from .store import Store
from .value import Signal, find_signals, kelly_stake, odds_state_gate

log = logging.getLogger(__name__)


class Agent:
    def __init__(self, provider, notifiers: list, store: Store, cfg: Config,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.provider, self.notifiers, self.store, self.cfg = provider, notifiers, store, cfg
        self.model = GoalModel(cfg.model)
        self.clock, self.sleep = clock, sleep
        self._last_settle_check: dict[int, float] = {}
        self._idle = False  # el último ciclo no encontró partidos en vivo

    # ---- ciclo ---------------------------------------------------------------------------
    def cycle(self) -> list[Signal]:
        now = self.clock()
        matches = self.provider.live_matches()
        self._idle = not matches
        self._settle({m.fixture_id for m in matches}, now)
        if not matches:
            return []  # sin partidos en juego no se piden cuotas: cada request cuenta contra la cuota diaria
        odds = self.provider.live_odds()

        vcfg, acfg = self.cfg.value, self.cfg.agent
        candidates: list[Signal] = []
        for m in matches:
            quotes = odds.get(m.fixture_id)
            if not quotes or m.status not in LIVE_STATUSES:
                continue
            if not (vcfg.min_minute <= m.elapsed <= vcfg.max_minute):
                continue  # antes de enriquecer: cada enrich cuesta requests
            if self.store.count_for_fixture(m.fixture_id) >= acfg.max_signals_per_match:
                continue
            odds_score = (getattr(self.provider, "odds_meta", {}).get(m.fixture_id) or {}).get("score")
            if odds_state_gate(m, odds_score) is not None:
                continue  # cayó un gol y las cuotas aún no lo reflejan: se reintenta en el próximo ciclo
            m = self.provider.enrich(m)
            if acfg.require_stats and not _has_stats(m):
                continue
            probs, _rates = build_probs(self.model, m, quotes)
            if probs is None:
                continue  # prior="market" y no hay Over/Under ni próximo gol completos para anclar
            candidates += [s for s in find_signals(m, quotes, probs, vcfg, now) if self._is_new(s, now)]

        candidates.sort(key=lambda s: s.ev, reverse=True)
        sent: list[Signal] = []
        for sig in candidates:
            if len(sent) >= acfg.max_alerts_per_cycle:
                break
            if self.store.count_for_fixture(sig.fixture_id) >= acfg.max_signals_per_match:
                continue
            confirmed = self._confirm(sig)
            if confirmed is None:
                continue
            self.store.add(confirmed, now)
            text = confirmed.describe(acfg.bankroll, acfg.currency, acfg.my_bookmaker)
            send_all(self.notifiers, text, f"Alerta gol: {confirmed.label()}")
            sent.append(confirmed)
        return sent

    def run_forever(self, stop: Callable[[], bool] = lambda: False) -> None:
        backoff = 0.0
        while not stop():
            try:
                self.cycle()
                backoff = 0.0
            except ProviderError as exc:
                backoff = min(max(backoff * 2, 15.0), 300.0)
                log.warning("proveedor: %s (reintento en %.0fs)", exc, self.cfg.agent.poll_seconds + backoff)
            except Exception:  # noqa: BLE001 - el servicio en vivo no debe morir por un ciclo malo
                backoff = min(max(backoff * 2, 15.0), 300.0)
                log.exception("error inesperado en el ciclo")
            self.sleep((self.cfg.agent.idle_poll_seconds if self._idle else self.cfg.agent.poll_seconds) + backoff)

    # ---- piezas --------------------------------------------------------------------------
    def _is_new(self, sig: Signal, now: float) -> bool:
        last = self.store.last_for_key(sig)
        if last is None:
            return True
        cooled = now - last["created_at"] >= self.cfg.agent.cooldown_min * 60
        better = sig.ev - last["ev"] >= self.cfg.agent.realert_ev_gain
        return cooled and better

    def _confirm(self, sig: Signal) -> Signal | None:
        """Re-consulta marcador, tarjetas y cuota justo antes de alertar; devuelve la señal con la cuota vigente."""
        try:
            fresh, quotes = self.provider.refresh(sig.fixture_id)
        except ProviderError as exc:
            log.info("confirmación falló para %s: %s", sig.fixture_id, exc)
            return None
        if fresh is None or fresh.status not in LIVE_STATUSES:
            return None
        if (fresh.home.goals, fresh.away.goals) != (sig.home_goals, sig.away_goals):
            return None  # cayó un gol entre lecturas: la cuota que vimos ya no vale
        q = next((q for q in quotes
                  if q.market is sig.market and q.selection == sig.selection and q.line == sig.line
                  and q.bookmaker == sig.bookmaker), None)
        if q is None or q.suspended:
            return None
        if q.odds < sig.odds * (1 - self.cfg.agent.confirm_odds_tolerance):
            return None
        ev = sig.p_final * q.odds - 1
        if ev < self.cfg.value.min_ev:
            return None
        v = self.cfg.value
        return replace(sig, odds=q.odds, ev=ev,
                       stake_pct=kelly_stake(sig.p_final, q.odds, v.kelly_fraction, v.max_stake_pct))

    def _settle(self, live_ids: set[int], now: float) -> None:
        results: dict[int, object] = {}
        for p in self.store.pending():
            if p.fixture_id in live_ids:
                continue
            if p.fixture_id not in results:
                if now - self._last_settle_check.get(p.fixture_id, -1e18) < self.cfg.agent.settle_check_s:
                    continue
                self._last_settle_check[p.fixture_id] = now
                try:
                    results[p.fixture_id] = self.provider.result(p.fixture_id)
                except ProviderError as exc:
                    log.info("liquidación %s: %s", p.fixture_id, exc)
                    results[p.fixture_id] = None
            res = results[p.fixture_id]
            if res is None or not res.finished:
                continue
            outcome = "void" if res.voided else outcome_for(
                p.market, p.selection, p.line, (p.home_goals, p.away_goals), (res.home_goals, res.away_goals))
            self.store.settle(p.id, outcome, now)


def _has_stats(m: LiveMatch) -> bool:
    return all(t.xg is not None or t.shots is not None or t.shots_on_target is not None
               for t in (m.home, m.away))

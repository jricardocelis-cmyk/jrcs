"""Línea de comandos: run | simulate | report | markets | test-notify."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from dataclasses import replace

from .agent import Agent
from .anchor import build_probs
from .model import GoalModel
from .config import Config, ConfigError, build_notifiers, build_provider, load_config
from .notifiers import ConsoleNotifier, send_all
from .providers.apifootball import ApiFootball
from .providers.sim import SimProvider
from .store import Store
from .value import evaluate_quotes, match_gate, selection_label


def print_report(store: Store) -> None:
    rep = store.report()
    print(f"\nSeñales liquidadas: {rep['all']['n']} · pendientes: {rep['pending']} · anuladas: {rep['void']}")
    rows = [("TODAS", rep["all"])] + sorted(rep["by_market"].items())
    if rep["all"]["n"] == 0:
        return
    print(f"{'mercado':<12}{'n':>5}{'acierto':>9}{'prob.media':>12}{'brier':>8}{'ROI plano':>11}")
    for name, a in rows:
        if a["n"]:
            print(f"{name:<12}{a['n']:>5}{a['hit_rate']:>9.1%}{a['avg_p']:>12.1%}{a['brier']:>8.3f}{a['roi_flat']:>+11.1%}")
    print("Calibrado = acierto ≈ prob.media. ROI plano = apostando 1 unidad por señal a la cuota alertada.")


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    provider, notifiers = build_provider(cfg), build_notifiers(cfg.notify)
    store = Store(cfg.agent.db_path)
    agent = Agent(provider, notifiers, store, cfg)
    logging.getLogger("goalalert").info(
        "agente en marcha: proveedor=%s ligas=%s poll=%ss", cfg.provider.name,
        list(cfg.provider.leagues) or "todas", cfg.agent.poll_seconds)
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        print("\ndetenido")
    return 0


def cmd_simulate(args) -> int:
    cfg = load_config(args.config) if args.config else Config()
    # El simulador no tiene equipos reales ni un mercado independiente: se usa el prior de liga para ver alertas.
    cfg = replace(cfg, agent=replace(cfg.agent, settle_check_s=0.0), model=replace(cfg.model, prior="league"))
    provider = SimProvider(n_matches=args.matches, seed=args.seed)
    store = Store(":memory:")
    ticks = 0
    notifiers = [] if args.quiet else [ConsoleNotifier()]
    agent = Agent(provider, notifiers, store, cfg, clock=lambda: ticks * 60.0)
    while not provider.all_finished():
        provider.tick()
        ticks += 1
        agent.cycle()
    agent.cycle()  # última pasada para liquidar los partidos terminados
    print_report(store)
    return 0


def cmd_report(args) -> int:
    cfg = load_config(args.config)
    print_report(Store(cfg.agent.db_path))
    return 0


def cmd_markets(args) -> int:
    cfg = load_config(args.config)
    provider = build_provider(cfg)
    if not isinstance(provider, ApiFootball):
        print("este comando solo aplica a api-football")
        return 1
    leagues, bets, samples = provider.inspect()
    print("Ligas con partidos en vivo ahora (id · nombre · país · partidos):")
    for (lid, name, country), n in sorted(leagues.items(), key=lambda kv: -kv[1]):
        print(f"  {lid:>5} · {name} · {country} · {n}")
    mapped = {n.strip().lower() for names in cfg.provider.bet_names.values() for n in names}
    print("\nMercados con cuotas en vivo (✓ = ya mapeado en provider.bet_names):")
    for name, n in sorted(bets.items(), key=lambda kv: -kv[1]):
        print(f"  {'✓' if name.strip().lower() in mapped else ' '} {name} ({n})")
    print("\nMuestra de valores crudos de los mercados de goles (primer partido que los trae):")
    for name, smp in samples.items():
        print(f"  {name}  [fixture {smp['fixture']}, marcador {smp['score']}]")
        for v in smp["values"]:
            print(f"      {json.dumps(v, ensure_ascii=False)}")
    return 0


def _fmt_stat(a, b) -> str:
    return "n/d" if a is None and b is None else f"{'?' if a is None else a}-{'?' if b is None else b}"


def cmd_snapshot(args) -> int:
    """Un ciclo en vivo sin enviar alertas: muestra qué calculó el modelo y qué filtro descartó cada apuesta."""
    cfg = load_config(args.config)
    provider = build_provider(cfg)
    if not isinstance(provider, ApiFootball):
        print("este comando solo aplica a api-football")
        return 1
    model, now = GoalModel(cfg.model), time.time()
    matches, odds = provider.live_matches(), provider.live_odds()
    live_ids = {m.fixture_id for m in matches}
    print(f"{len(matches)} partidos en vivo · {sum(1 for i in odds if i in live_ids)} con cuotas de mercados de goles leídas"
          f" · requests restantes hoy: {provider.remaining_requests if provider.remaining_requests is not None else 'n/d'}")
    print(f"desfase del reloj de tu PC respecto al servidor de la API: {provider.clock_skew_s:+.0f} s"
          f"{'  (la edad de las cuotas ya se corrige con esto)' if abs(provider.clock_skew_s) > 5 else ''}")
    # El diagnóstico muestra los números aunque la cuota sea "vieja" y reporta la edad aparte.
    diag_cfg = replace(cfg.value, max_odds_age_s=0.0)
    for m in matches:
        quotes = odds.get(m.fixture_id, [])
        print(f"\n{m.league_name} ({m.country}) · {m.home.name} {m.home.goals}-{m.away.goals} {m.away.name}"
              f" · {m.status} {m.elapsed}'")
        if not quotes:
            print("  sin cuotas de over/under, ambos anotan o próximo gol para este partido")
            continue
        read = Counter(q.market.value for q in quotes)
        print("  cuotas leídas: " + ", ".join(f"{k}={v}" for k, v in sorted(read.items())))
        ages = [now - q.updated_at for q in quotes if q.updated_at is not None]
        raw_update = (provider.odds_meta.get(m.fixture_id) or {}).get("update")
        if ages:
            limit = cfg.value.max_odds_age_s
            verdict = f" → el agente las descartaría (límite {limit:.0f} s)" if limit > 0 and max(ages) > limit else ""
            print(f"  hora de las cuotas (campo update de la API): {raw_update} · hace {max(ages):.0f} s{verdict}")
        else:
            print(f"  hora de las cuotas (campo update de la API): {raw_update!r} · no interpretable: no se valida edad")
        if not args.no_stats:
            m = provider.enrich(m)
        print(f"  tiros {_fmt_stat(m.home.shots, m.away.shots)} · a puerta "
              f"{_fmt_stat(m.home.shots_on_target, m.away.shots_on_target)} · xG {_fmt_stat(m.home.xg, m.away.xg)}"
              f" · rojas {m.home.red_cards}-{m.away.red_cards}{'' if m.red_known else ' (sin eventos: no verificadas)'}")
        gate = match_gate(m, cfg.value)
        if gate:
            print(f"  fuera de juego para alertas: {gate}")
        probs, rates = build_probs(model, m, quotes)
        if probs is None:
            print("  sin ancla de mercado (falta un Over/Under o un próximo gol completo): no se evalúa con prior=\"market\"")
            continue
        if rates is not None:
            print(f"  ancla de mercado: local {rates.lam_home:.2f} / visita {rates.lam_away:.2f} ({rates.source})")
        print(f"  goles esperados restantes{' tras ajustar por presión' if rates else ''}: "
              f"local {probs.lam_home:.2f} / visita {probs.lam_away:.2f}")
        evals = evaluate_quotes(m, quotes, probs, diag_cfg, now)
        for e in sorted(evals, key=lambda e: (e.market.value, e.line or 0.0, e.selection)):
            label = selection_label(e.market, e.selection, e.line, m.home.name, m.away.name)
            if e.p_model is None:
                nums = ""
            else:
                nums = f"modelo {e.p_model:.0%} · mercado {e.p_market:.0%}"
                if e.ev is not None:
                    nums += f" · EV {e.ev:+.1%}"
            verdict = "ALERTA (si el minuto lo permite)" if e.reason is None else f"descartada: {e.reason}"
            print(f"    {label:<28} @{e.odds:<6.2f} {nums:<42} → {verdict}")
    return 0


def cmd_coverage(args) -> int:
    """Cobertura declarada por la API para una liga: ¿hay estadísticas, eventos y cuotas?"""
    cfg = load_config(args.config)
    provider = build_provider(cfg)
    if not isinstance(provider, ApiFootball):
        print("este comando solo aplica a api-football")
        return 1
    ids = args.league or list(cfg.provider.leagues) or [281]
    for lid in ids:
        info = provider.coverage(lid)
        if info is None:
            print(f"liga {lid}: la API no la reconoce")
            continue
        cov = info["coverage"]
        fx = cov.get("fixtures") or {}
        print(f"liga {lid}: {info['name']} ({info['country']}) · temporada {info['season']}")
        print(f"  estadísticas por partido (tiros, xG): {fx.get('statistics_fixtures', 'n/d')}")
        print(f"  eventos (goles, tarjetas):            {fx.get('events', 'n/d')}")
        print(f"  cuotas:                               {cov.get('odds', 'n/d')}  (cobertura declarada; las cuotas en vivo pueden faltar igual)")
        print(f"  cobertura completa: {json.dumps(cov, ensure_ascii=False)}")
    return 0


def cmd_schedule(args) -> int:
    """Próximos partidos de una liga (hora de Lima), para saber cuándo probar en vivo."""
    cfg = load_config(args.config)
    provider = build_provider(cfg)
    if not isinstance(provider, ApiFootball):
        print("este comando solo aplica a api-football")
        return 1
    for lid in args.league or list(cfg.provider.leagues) or [281]:
        games = provider.upcoming(lid, args.n)
        print(f"liga {lid}: {games[0]['league'] if games else 'sin partidos próximos'}")
        for g in games:
            print(f"  {g['when']} (hora de Lima) · {g['home']} vs {g['away']} · {g['status']}")
    return 0


def cmd_test_notify(args) -> int:
    cfg = load_config(args.config)
    ok = send_all(build_notifiers(cfg.notify), "Prueba de goalalert: los canales funcionan.", "Prueba goalalert")
    print(f"canales OK: {ok}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="goalalert", description="Alertas de valor en goles de fútbol en vivo")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn, helptext in (
        ("run", cmd_run, "agente en vivo"),
        ("report", cmd_report, "calibración y ROI de las señales registradas"),
        ("markets", cmd_markets, "ligas y mercados que devuelve tu cuenta de API-Football"),
        ("test-notify", cmd_test_notify, "enviar un mensaje de prueba por los canales configurados"),
        ("snapshot", cmd_snapshot, "un ciclo en vivo sin alertas: qué calcula el modelo y qué filtro descarta cada apuesta"),
        ("coverage", cmd_coverage, "cobertura declarada de una liga: estadísticas, eventos y cuotas"),
        ("schedule", cmd_schedule, "próximos partidos de una liga (hora de Lima)"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("-c", "--config", required=True)
        if name == "snapshot":
            p.add_argument("--no-stats", action="store_true", help="no pedir tiros/xG/tarjetas (ahorra requests)")
        if name in ("coverage", "schedule"):
            p.add_argument("--league", type=int, action="append", help="id de liga (repetible); por defecto 281 (Liga 1 de Perú)")
        if name == "schedule":
            p.add_argument("--n", type=int, default=5, help="cuántos partidos próximos mostrar")
        p.set_defaults(fn=fn)
    p = sub.add_parser("simulate", help="sesión simulada de punta a punta (sin API key)")
    p.add_argument("-c", "--config")
    p.add_argument("--matches", type=int, default=8)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--quiet", action="store_true", help="solo el reporte final")
    p.set_defaults(fn=cmd_simulate)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.fn(args)
    except ConfigError as exc:
        print(f"error de configuración: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Línea de comandos: run | simulate | report | markets | test-notify."""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace

from .agent import Agent
from .config import Config, ConfigError, build_notifiers, build_provider, load_config
from .notifiers import ConsoleNotifier, send_all
from .providers.apifootball import ApiFootball
from .providers.sim import SimProvider
from .store import Store


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
    cfg = replace(cfg, agent=replace(cfg.agent, settle_check_s=0.0))
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
    leagues, bets = provider.inspect()
    print("Ligas con partidos en vivo ahora (id · nombre · país · partidos):")
    for (lid, name, country), n in sorted(leagues.items(), key=lambda kv: -kv[1]):
        print(f"  {lid:>5} · {name} · {country} · {n}")
    mapped = {n.strip().lower() for names in cfg.provider.bet_names.values() for n in names}
    print("\nMercados con cuotas en vivo (✓ = ya mapeado en provider.bet_names):")
    for name, n in sorted(bets.items(), key=lambda kv: -kv[1]):
        print(f"  {'✓' if name.strip().lower() in mapped else ' '} {name} ({n})")
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
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("-c", "--config", required=True)
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

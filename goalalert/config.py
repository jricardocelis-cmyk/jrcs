"""Carga de configuración desde TOML. Los secretos se leen solo de variables de entorno."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .model import ModelParams
from .notifiers import ConsoleNotifier, NtfyNotifier, TelegramNotifier
from .providers.apifootball import DEFAULT_BET_NAMES, ApiFootball
from .providers.sim import SimProvider
from .value import ValueConfig


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AgentConfig:
    poll_seconds: float = 30.0
    idle_poll_seconds: float = 120.0  # espera cuando no hay partidos en vivo (1 request por ciclo)
    db_path: str = "goalalert.db"
    cooldown_min: float = 10.0  # mismo pick: espera mínima para volver a alertar
    realert_ev_gain: float = 0.04  # ...y solo si el EV mejoró al menos esto
    max_signals_per_match: int = 2
    max_alerts_per_cycle: int = 3
    confirm_odds_tolerance: float = 0.03  # la cuota re-consultada no puede caer más que esto
    settle_check_s: float = 300.0
    require_stats: bool = True  # sin tiros/xG el modelo solo tiene el prior: no alertar
    bankroll: float = 0.0
    currency: str = ""
    my_bookmaker: str = ""  # casa donde apuestas: aparece en la línea de cuota mínima de cada alerta


@dataclass(frozen=True)
class ProviderConfig:
    name: str = "api-football"  # "api-football" | "sim"
    leagues: tuple[int, ...] = ()  # vacío = todas las ligas en vivo
    stats_ttl_s: float = 120.0
    max_enrich_per_cycle: int = 15
    bet_names: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_BET_NAMES))


@dataclass(frozen=True)
class NotifyConfig:
    console: bool = True
    telegram: bool = False  # TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en el entorno
    ntfy: bool = False  # NTFY_TOPIC en el entorno
    ntfy_server: str = "https://ntfy.sh"


@dataclass(frozen=True)
class Config:
    agent: AgentConfig = field(default_factory=AgentConfig)
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    value: ValueConfig = field(default_factory=ValueConfig)
    model: ModelParams = field(default_factory=ModelParams)
    notify: NotifyConfig = field(default_factory=NotifyConfig)


def _coerce(cls, raw: dict, where: str):
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"[{where}] claves desconocidas: {', '.join(sorted(unknown))}")
    kwargs = dict(raw)
    if cls is ProviderConfig:
        if "leagues" in kwargs:
            kwargs["leagues"] = tuple(int(x) for x in kwargs["leagues"])
        if "bet_names" in kwargs:
            merged = dict(DEFAULT_BET_NAMES)
            merged.update({k: tuple(v) for k, v in kwargs["bet_names"].items()})
            kwargs["bet_names"] = merged
    if cls is ValueConfig and "markets" in kwargs:
        kwargs["markets"] = tuple(kwargs["markets"])
    if cls is ModelParams:
        if "league_priors" in kwargs:
            kwargs["league_priors"] = {str(k): (float(v[0]), float(v[1])) for k, v in kwargs["league_priors"].items()}
        if "score_mult" in kwargs:
            base = ModelParams().score_mult
            base.update({int(k): float(v) for k, v in kwargs["score_mult"].items()})
            kwargs["score_mult"] = base
    if cls is ModelParams and kwargs.get("prior", "market") not in ("market", "league"):
        raise ConfigError(f"[{where}] prior debe ser \"market\" o \"league\", no {kwargs['prior']!r}")
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"[{where}] {exc}") from exc


def load_config(path: str | Path | None) -> Config:
    if path is None:
        return Config()
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    unknown = set(raw) - {"agent", "provider", "value", "model", "notify"}
    if unknown:
        raise ConfigError(f"secciones desconocidas: {', '.join(sorted(unknown))}")
    return Config(
        agent=_coerce(AgentConfig, raw.get("agent", {}), "agent"),
        provider=_coerce(ProviderConfig, raw.get("provider", {}), "provider"),
        value=_coerce(ValueConfig, raw.get("value", {}), "value"),
        model=_coerce(ModelParams, raw.get("model", {}), "model"),
        notify=_coerce(NotifyConfig, raw.get("notify", {}), "notify"),
    )


def build_provider(cfg: Config):
    p = cfg.provider
    if p.name == "sim":
        return SimProvider()
    if p.name != "api-football":
        raise ConfigError(f"provider.name desconocido: {p.name}")
    key = os.environ.get("APIFOOTBALL_KEY")
    if not key:
        raise ConfigError("falta la variable de entorno APIFOOTBALL_KEY")
    return ApiFootball(
        api_key=key, league_ids=p.leagues, bet_names=p.bet_names,
        stats_ttl_s=p.stats_ttl_s, max_enrich_per_cycle=p.max_enrich_per_cycle,
    )


def build_notifiers(cfg: NotifyConfig) -> list:
    out = []
    if cfg.console:
        out.append(ConsoleNotifier())
    if cfg.telegram:
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            raise ConfigError("telegram=true requiere TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en el entorno")
        out.append(TelegramNotifier(token, chat))
    if cfg.ntfy:
        topic = os.environ.get("NTFY_TOPIC")
        if not topic:
            raise ConfigError("ntfy=true requiere NTFY_TOPIC en el entorno")
        out.append(NtfyNotifier(topic, cfg.ntfy_server))
    return out

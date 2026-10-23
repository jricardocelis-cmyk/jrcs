from pathlib import Path

import pytest

from goalalert.config import ConfigError, build_notifiers, build_provider, load_config
from goalalert.providers.apifootball import ApiFootball

EXAMPLE = Path(__file__).parent.parent / "config.example.toml"


def write(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text)
    return p


def test_example_config_loads_with_defaults():
    cfg = load_config(EXAMPLE)
    assert cfg.provider.name == "api-football" and cfg.provider.leagues == ()
    assert cfg.value.markets == ("over_under", "btts", "next_goal")
    assert cfg.model.score_mult[-1] == 1.10


def test_overrides_and_conversions(tmp_path):
    cfg = load_config(write(tmp_path, """
[provider]
leagues = [281, 39]
[provider.bet_names]
btts = ["both teams score"]
[model.league_priors]
"281" = [1.3, 1.0]
[model.score_mult]
"-1" = 1.2
"""))
    assert cfg.provider.leagues == (281, 39)
    assert cfg.provider.bet_names["btts"] == ("both teams score",)
    assert "over_under" in cfg.provider.bet_names  # lo no tocado conserva el valor por defecto
    assert cfg.model.priors_for(281) == (1.3, 1.0) and cfg.model.score_mult[-1] == 1.2 and cfg.model.score_mult[1] == 0.92


def test_typos_fail_loudly(tmp_path):
    with pytest.raises(ConfigError, match="min_evv"):
        load_config(write(tmp_path, "[value]\nmin_evv = 0.1\n"))
    with pytest.raises(ConfigError, match="secciones"):
        load_config(write(tmp_path, "[valu]\nmin_ev = 0.1\n"))


def test_secrets_come_from_env(tmp_path, monkeypatch):
    monkeypatch.delenv("APIFOOTBALL_KEY", raising=False)
    with pytest.raises(ConfigError, match="APIFOOTBALL_KEY"):
        build_provider(load_config(EXAMPLE))
    monkeypatch.setenv("APIFOOTBALL_KEY", "k")
    assert isinstance(build_provider(load_config(EXAMPLE)), ApiFootball)

    cfg = load_config(write(tmp_path, "[notify]\ntelegram = true\n"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="TELEGRAM"):
        build_notifiers(cfg.notify)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    assert len(build_notifiers(cfg.notify)) == 2  # consola + telegram


def test_prior_must_be_market_or_league(tmp_path):
    assert load_config(EXAMPLE).model.prior == "market"
    assert load_config(write(tmp_path, '[model]\nprior = "league"\n')).model.prior == "league"
    with pytest.raises(ConfigError, match="prior"):
        load_config(write(tmp_path, '[model]\nprior = "mercado"\n'))

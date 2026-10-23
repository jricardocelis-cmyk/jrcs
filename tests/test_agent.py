from dataclasses import replace

from goalalert.agent import Agent
from goalalert.config import AgentConfig, Config
from goalalert.model import ModelParams
from goalalert.models import FinalResult, LiveMatch, Market, OVER, UNDER, Quote, TeamLive
from goalalert.notifiers import send_all
from goalalert.providers.sim import SimProvider
from goalalert.store import Store
from goalalert.value import ValueConfig


class Capture:
    def __init__(self):
        self.messages = []

    def send(self, text, title=""):
        self.messages.append((title, text))


class Boom:
    def send(self, text, title=""):
        raise RuntimeError("canal caído")


class FakeProvider:
    """Un partido 0-0 al 30' con poca presión: modelo ~80% de Over 0.5 contra ~66% del mercado."""

    def __init__(self):
        self.match = LiveMatch(1, 281, "Liga 1", "Peru", "1H", 30,
                               TeamLive("A", 1, 0, 0, shots=2, shots_on_target=0),
                               TeamLive("B", 2, 0, 0, shots=3, shots_on_target=1))
        self.live = True
        self.over, self.under = 1.50, 2.90
        self.refresh_over = None  # None => la misma cuota
        self.refresh_goal = False
        self.final = None

    def _quotes(self, over):
        return [Quote(1, Market.OVER_UNDER, OVER, 0.5, over, bookmaker="fake"),
                Quote(1, Market.OVER_UNDER, UNDER, 0.5, self.under, bookmaker="fake")]

    def live_matches(self):
        return [self.match] if self.live else []

    def live_odds(self):
        return {1: self._quotes(self.over)} if self.live else {}

    def enrich(self, m):
        return m

    def refresh(self, fid):
        m = self.match
        if self.refresh_goal:
            m = replace(m, home=replace(m.home, goals=1))
        return m, self._quotes(self.refresh_over if self.refresh_over is not None else self.over)

    def result(self, fid):
        return self.final


def make_agent(provider, now=[0.0], **agent_kw):
    cfg = Config(agent=AgentConfig(settle_check_s=0.0, **agent_kw), value=ValueConfig(min_ev=0.0, min_prob=0.5, max_divergence=0.5),
                 model=ModelParams(prior="league"))  # la guarda de divergencia se prueba en test_value
    out = Capture()
    return Agent(provider, [out], Store(":memory:"), cfg, clock=lambda: now[0]), out, now


def test_alerts_once_and_stores_signal():
    agent, out, _ = make_agent(FakeProvider())
    sent = agent.cycle()
    assert len(sent) == 1 and sent[0].selection == OVER
    assert len(out.messages) == 1 and "Más de 0.5 goles" in out.messages[0][1]
    assert agent.store.count_for_fixture(1) == 1
    assert agent.cycle() == []  # mismo pick: deduplicado
    assert len(out.messages) == 1


def test_realert_needs_cooldown_and_better_ev():
    prov = FakeProvider()
    agent, out, now = make_agent(prov, cooldown_min=10.0, realert_ev_gain=0.04, max_signals_per_match=5)
    agent.cycle()
    prov.over = 1.70
    now[0] += 60
    assert agent.cycle() == []  # mejor EV pero sin enfriar
    now[0] += 11 * 60
    assert len(agent.cycle()) == 1  # enfrió y el EV subió


def test_alert_names_the_users_bookmaker():
    agent, out, _ = make_agent(FakeProvider(), my_bookmaker="Te Apuesto")
    agent.cycle()
    assert "EN TE APUESTO: apuesta solo si paga" in out.messages[0][1]


def test_goal_between_reads_blocks_alert():
    prov = FakeProvider()
    prov.refresh_goal = True
    agent, out, _ = make_agent(prov)
    assert agent.cycle() == [] and out.messages == []


def test_dropped_odds_on_reconfirm_block_alert_but_small_move_updates_it():
    prov = FakeProvider()
    prov.refresh_over = 1.40  # cayó ~7%
    agent, out, _ = make_agent(prov)
    assert agent.cycle() == []
    prov.refresh_over = 1.47  # cayó 2%: se alerta con la cuota vigente
    sent = agent.cycle()
    assert len(sent) == 1 and sent[0].odds == 1.47
    assert "@ 1.47" in out.messages[0][1]


def test_require_stats_blocks_alert_without_shots():
    prov = FakeProvider()
    prov.match = replace(prov.match, home=TeamLive("A", 1), away=TeamLive("B", 2))
    agent, out, _ = make_agent(prov)
    assert agent.cycle() == []
    agent2, out2, _ = make_agent(prov, require_stats=False)
    assert len(agent2.cycle()) == 1


def test_settlement_after_match_leaves_live_list():
    prov = FakeProvider()
    agent, _, _ = make_agent(prov)
    agent.cycle()
    prov.live = False
    prov.final = FinalResult(1, 1, 0, finished=True)
    agent.cycle()
    assert agent.store.report()["all"]["n"] == 1 and agent.store.report()["all"]["hit_rate"] == 1.0
    assert agent.store.pending() == []


def test_voided_fixture_is_settled_as_void():
    prov = FakeProvider()
    agent, _, _ = make_agent(prov)
    agent.cycle()
    prov.live = False
    prov.final = FinalResult(1, 0, 0, finished=True, voided=True)
    agent.cycle()
    assert agent.store.report()["void"] == 1


def test_one_dead_channel_does_not_block_the_others():
    good = Capture()
    assert send_all([Boom(), good], "hola", "t") == 1
    assert good.messages == [("t", "hola")]


def test_simulated_session_runs_end_to_end_and_settles_everything():
    cfg = Config(agent=AgentConfig(settle_check_s=0.0), model=ModelParams(prior="league"))
    provider = SimProvider(n_matches=10, seed=7)
    store, ticks = Store(":memory:"), [0]
    out = Capture()
    agent = Agent(provider, [out], store, cfg, clock=lambda: ticks[0] * 60.0)
    while not provider.all_finished():
        provider.tick()
        ticks[0] += 1
        agent.cycle()
    agent.cycle()
    rep = store.report()
    assert len(out.messages) >= 1
    assert rep["pending"] == 0
    assert rep["all"]["n"] + rep["void"] == len(out.messages)
    per_match = {}
    for p in store.db.execute("SELECT fixture_id, COUNT(*) c FROM signals GROUP BY fixture_id"):
        per_match[p["fixture_id"]] = p["c"]
    assert max(per_match.values()) <= cfg.agent.max_signals_per_match


def test_market_prior_does_not_alert_when_live_evidence_agrees_with_the_market():
    prov = FakeProvider()  # solo Over/Under 0.5: ancla total + reparto de liga; poca presión de tiros
    cfg = Config(agent=AgentConfig(settle_check_s=0.0), value=ValueConfig(min_ev=0.06),
                 model=ModelParams(prior="market"))
    out = Capture()
    agent = Agent(prov, [out], Store(":memory:"), cfg, clock=lambda: 0.0)
    assert agent.cycle() == [] and out.messages == []  # el modelo parte del mercado: sin valor, sin alerta


def test_market_prior_skips_matches_without_a_usable_anchor():
    prov = FakeProvider()
    prov.live_odds = lambda: {1: []}  # el partido está en vivo pero sin cuotas utilizables
    cfg = Config(agent=AgentConfig(settle_check_s=0.0), model=ModelParams(prior="market"))
    agent = Agent(prov, [Capture()], Store(":memory:"), cfg, clock=lambda: 0.0)
    assert agent.cycle() == []


def test_idle_cycle_skips_the_odds_request_but_still_settles():
    prov = FakeProvider()
    agent, _, _ = make_agent(prov)
    agent.cycle()
    prov.live = False
    prov.final = FinalResult(1, 1, 0, finished=True)
    asked = []
    prov.live_odds = lambda: asked.append("odds") or {}
    agent.cycle()
    assert asked == []  # sin partidos en vivo no se piden cuotas
    assert agent.store.report()["all"]["n"] == 1  # ...pero la liquidación sí corrió


def test_run_forever_waits_longer_when_nothing_is_live():
    sleeps = []
    prov = FakeProvider()
    cfg = Config(agent=AgentConfig(poll_seconds=30.0, idle_poll_seconds=120.0, settle_check_s=0.0), model=ModelParams(prior="league"))
    agent = Agent(prov, [Capture()], Store(":memory:"), cfg, clock=lambda: 0.0, sleep=sleeps.append)
    agent.run_forever(stop=lambda: len(sleeps) >= 1)
    assert sleeps == [30.0]  # hay partido en vivo
    prov.live = False
    agent.run_forever(stop=lambda: len(sleeps) >= 2)
    assert sleeps == [30.0, 120.0]  # nadie juega: espera larga y 1 request por ciclo

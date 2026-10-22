"""Registro SQLite de señales: deduplicación, liquidación y reporte de calibración."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .value import Signal

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    fixture_id INTEGER NOT NULL,
    league TEXT, home TEXT, away TEXT,
    minute INTEGER, home_goals INTEGER, away_goals INTEGER,
    market TEXT NOT NULL, selection TEXT NOT NULL, line REAL,
    odds REAL NOT NULL, bookmaker TEXT,
    p_model REAL, p_market REAL, p_final REAL, ev REAL, stake_pct REAL,
    outcome TEXT, settled_at REAL
);
CREATE INDEX IF NOT EXISTS idx_signals_fixture ON signals(fixture_id);
"""


@dataclass(frozen=True)
class PendingSignal:
    id: int
    fixture_id: int
    market: str
    selection: str
    line: float | None
    home_goals: int
    away_goals: int


class Store:
    def __init__(self, path: str = "goalalert.db"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def add(self, s: Signal, now: float) -> int:
        cur = self.db.execute(
            "INSERT INTO signals (created_at, fixture_id, league, home, away, minute, home_goals,"
            " away_goals, market, selection, line, odds, bookmaker, p_model, p_market, p_final, ev,"
            " stake_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, s.fixture_id, s.league, s.home, s.away, s.minute, s.home_goals, s.away_goals,
             s.market.value, s.selection, s.line, s.odds, s.bookmaker, s.p_model, s.p_market,
             s.p_final, s.ev, s.stake_pct),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def last_for_key(self, s: Signal) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM signals WHERE fixture_id=? AND market=? AND selection=? AND line IS ?"
            " ORDER BY id DESC LIMIT 1",
            (s.fixture_id, s.market.value, s.selection, s.line),
        ).fetchone()

    def count_for_fixture(self, fixture_id: int) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM signals WHERE fixture_id=?", (fixture_id,)
        ).fetchone()[0]

    def pending(self) -> list[PendingSignal]:
        rows = self.db.execute(
            "SELECT id, fixture_id, market, selection, line, home_goals, away_goals"
            " FROM signals WHERE outcome IS NULL"
        ).fetchall()
        return [PendingSignal(**dict(r)) for r in rows]

    def settle(self, signal_id: int, outcome: str, now: float) -> None:
        self.db.execute(
            "UPDATE signals SET outcome=?, settled_at=? WHERE id=?", (outcome, now, signal_id)
        )
        self.db.commit()

    def report(self) -> dict:
        rows = self.db.execute(
            "SELECT market, odds, p_final, outcome FROM signals WHERE outcome IN ('won','lost')"
        ).fetchall()
        pending = self.db.execute("SELECT COUNT(*) FROM signals WHERE outcome IS NULL").fetchone()[0]
        voids = self.db.execute("SELECT COUNT(*) FROM signals WHERE outcome='void'").fetchone()[0]

        def agg(sel: list[sqlite3.Row]) -> dict:
            n = len(sel)
            if n == 0:
                return {"n": 0}
            wins = [r["outcome"] == "won" for r in sel]
            return {
                "n": n,
                "hit_rate": sum(wins) / n,
                "avg_p": sum(r["p_final"] for r in sel) / n,
                "brier": sum((r["p_final"] - w) ** 2 for r, w in zip(sel, wins)) / n,
                "roi_flat": sum((r["odds"] - 1) if w else -1.0 for r, w in zip(sel, wins)) / n,
            }

        by_market = {m: agg([r for r in rows if r["market"] == m]) for m in {r["market"] for r in rows}}
        return {"all": agg(rows), "by_market": by_market, "pending": pending, "void": voids}

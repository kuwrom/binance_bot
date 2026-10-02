"""Book-level risk rules for several concurrent shorts (book.py, learning #30)."""

import sqlite3

from shortbot.book import book_guard, diversity_mult, pending_book, slot_cap
from shortbot.config import RiskLimits
from shortbot.risk_budget import BudgetInputs, compute_budget

LIM5 = RiskLimits(risk_per_trade_pct=1.0, max_drawdown_pct=20, max_portfolio_heat_pct=5, max_cluster_heat_pct=3.5,
                  max_positions=5)
SYM = "PUMP/USDT:USDT"


def b(**kw):
    base = dict(equity=10_000, peak_equity=10_000, regime="favorable")
    base.update(kw)
    return compute_budget(LIM5, BudgetInputs(**base), SYM)


def test_a_new_trade_gets_one_slot_of_the_cluster_budget():
    assert slot_cap(10_000, LIM5, "alts") == 70             # 3.5% / 5 slots
    assert slot_cap(10_000, LIM5, "major:BTC") == 100       # majors: portfolio cap 5% / 5
    r = b()
    assert r["risk_usd"] == 70 and r["slot_cap_usd"] == 70
    assert b(regime="caution")["risk_usd"] == 70            # 75 before the slot cap


def test_resting_entries_of_other_symbols_count_as_heat():
    r = b(pending_risks={"OTHER/USDT:USDT": 330})          # cluster cap 350 -> 20 room
    assert r["risk_usd"] == 20 and r["pending_heat_pct"] == 3.3
    assert b(pending_risks={"OTHER/USDT:USDT": 400})["blocked"]


def test_a_repeated_driver_is_half_size():
    book = [{"symbol": f"C{i}/USDT:USDT", "setup": "pump_fade", "sector": "AI"} for i in range(2)]
    r = b(book=book, setup="pump_fade", sector="AI")
    assert r["multipliers"]["m_div"] == 0.5 and r["risk_usd"] == 50 and "driver overlap" in r["reasons"][0]
    assert b(book=book, setup="pump_fade", sector="DeFi")["multipliers"]["m_div"] == 1.0
    assert diversity_mult(book[:1], SYM, "pump_fade", "AI") == (1.0, "")
    assert diversity_mult(book, SYM, None, "AI")[0] == 1.0   # unknown driver: no cut


def test_no_new_alt_risk_into_a_market_pause_with_three_alt_shorts_open():
    opn = {f"C{i}/USDT:USDT": 10 for i in range(3)}
    pause = ["BTC +1.10% in 15 min: alts catch up, wait"]
    r = b(open_risks=opn, pause=pause)
    assert r["blocked"] and any("market pause" in x for x in r["reasons"])
    assert not b(open_risks=dict(list(opn.items())[:2]), pause=pause)["blocked"]
    assert book_guard(3, []) is None


def _db():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, group_id TEXT, symbol TEXT, status TEXT, setup TEXT,"
               " amount REAL, limit_price REAL, stop_price REAL, tranche TEXT)")
    rows = [  # group g1: two OCO alternatives -> worst single order; group g2: a split tranche -> legs add up
        ("g1", "A/USDT:USDT", "pending", "pump_fade", 100, 1.0, 1.1, None),
        ("g1", "B/USDT:USDT", "pending", "pump_fade", 100, 1.0, 1.3, None),
        ("g2", "C/USDT:USDT", "placing", "pump_fade", 100, 1.0, 1.1, "t1"),
        ("g2", "C/USDT:USDT", "unknown", "pump_fade", 100, 1.05, 1.1, "t1"),
        ("g3", "D/USDT:USDT", "filled", "pump_fade", 100, 1.0, 1.5, None),   # filled: an open position now
        ("g4", "PUMP/USDT:USDT", "pending", "pump_fade", 100, 1.0, 1.5, None),  # same symbol: its own legs
    ]
    db.executemany("INSERT INTO entries (group_id, symbol, status, setup, amount, limit_price, stop_price, tranche)"
                   " VALUES (?,?,?,?,?,?,?,?)", rows)
    return db


def test_pending_book_counts_each_group_once_at_its_worst_unit():
    risks, book = pending_book(_db(), SYM)
    assert set(risks) == {"B/USDT:USDT", "C/USDT:USDT"}
    assert abs(risks["B/USDT:USDT"] - 30) < 1e-9 and abs(risks["C/USDT:USDT"] - 15) < 1e-9
    assert sorted(x["symbol"] for x in book) == ["B/USDT:USDT", "C/USDT:USDT"]


def test_pending_book_tolerates_a_missing_table():
    assert pending_book(sqlite3.connect(":memory:"), SYM) == ({}, [])


def test_effective_bets_shrink_with_correlation():
    from shortbot.book import n_eff
    assert n_eff(5, 0.0) == 5 and abs(n_eff(5, 0.5) - 5 / 3) < 1e-9 and n_eff(5, 1.0) == 1
    assert n_eff(3, None) is None and n_eff(4, -0.2) == 4          # negative rho is not credited


def test_avg_corr_on_aligned_returns():
    from shortbot.book import avg_corr
    a = [0.01 * ((i % 7) - 3) for i in range(60)]
    assert abs(avg_corr({"A": a, "B": [2 * x for x in a]}) - 1.0) < 1e-9
    assert abs(avg_corr({"A": a, "B": [-x for x in a]}) + 1.0) < 1e-9
    assert avg_corr({"A": a}) is None and avg_corr({"A": a, "B": a[:10]}) is None   # too short


async def test_measured_corr_fetches_once_and_never_raises():
    from shortbot.book import _corr_cache, measured_corr

    class Pub:
        calls = 0

        async def fetch_ohlcv(self, s, tf, limit=0):
            Pub.calls += 1
            if s.startswith("BAD"):
                raise RuntimeError("no data")
            return [[i, 0, 0, 0, 100 + (i % 5) * (2 if s.startswith("X") else 1)] for i in range(limit)]
    _corr_cache.clear()
    p = Pub()
    rho = await measured_corr(p, ["X/USDT:USDT", "Y/USDT:USDT", "BAD/USDT:USDT"])
    assert rho is not None and rho > 0.9 and Pub.calls == 3
    assert await measured_corr(p, ["Y/USDT:USDT", "BAD/USDT:USDT", "X/USDT:USDT"]) == rho and Pub.calls == 3
    assert await measured_corr(p, ["X/USDT:USDT"]) is None


def test_budget_reports_the_book_it_would_join():
    book = [{"symbol": "A/USDT:USDT", "setup": "pump_fade", "sector": "AI"},
            {"symbol": "B/USDT:USDT", "setup": "breakdown", "sector": "DeFi"}]
    r = b(book=book, book_rho=0.5)
    assert r["book"] == {"n": 3, "rho": 0.5, "n_eff": 1.5}
    assert b()["book"] == {"n": 1, "rho": None, "n_eff": None}


def test_pending_book_counts_only_this_accounts_mode():
    db = _db()
    db.execute("ALTER TABLE entries ADD COLUMN mode TEXT DEFAULT 'demo'")
    db.execute("INSERT INTO entries (group_id, symbol, status, setup, amount, limit_price, stop_price, mode)"
               " VALUES ('g9','L/USDT:USDT','pending','pump_fade',100,1.0,1.2,'live')")
    demo, _ = pending_book(db, SYM, "demo")
    live, _ = pending_book(db, SYM, "live")
    everything, _ = pending_book(db, SYM)
    assert "L/USDT:USDT" not in demo and set(live) == {"L/USDT:USDT"} and "L/USDT:USDT" in everything
    assert abs(live["L/USDT:USDT"] - 20) < 1e-9

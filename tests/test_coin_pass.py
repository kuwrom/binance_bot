"""One coin-memory pass (coin_pass.py) as the guardian's `coins` lane runs it (guardian_coins.py): public data through
DataHub, states saved in one journal unit (never a write on the loop outside one), coin_event and the urgent
break_watch alerted but nothing closed, expired reads scored; the fake-out p90 from the sweep study; the tool's
refresh=True runs the same pass."""

import json
from datetime import datetime, timezone

from shortbot import alerts_view, clock, cmd_desk, coin_pass, guardian, sweeps, weight
from shortbot import coin_memory as cm
from shortbot.guardian_loop import Job, Lane
from shortbot.journal import Journal
from tests.fakes import SYM, FakeCcxt
from tests.loop_guard import connections, loop_writes
from tests.test_coin_stats import M5, M15, saw
from tests.test_guardian import guardian_with
from tests.test_guardian_offloop import guardian_on
from tests.test_untrusted import EVIL

NOW = 1_790_000_000_000 // M15 * M15 + 7 * 60_000
H = 3_600_000
QNT, BTC = "QNT/USDT:USDT", "BTC/USDT:USDT"
RISE = [1.0 + 0.0067 * k for k in range(1, 13)]          # the last hour: +8% against the short


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def bars(closes, step=M5, now=NOW):
    t0 = now // step * step - (len(closes) - 1) * step
    return [[t0 + i * step, c, c * 1.001, c * 0.999, c, 10.0] for i, c in enumerate(closes)]


class Market:
    """Public data as ccxt / Exchanges return it: SYM breaks out against a short (price, OI and taker buyers up, BTC
    +1% in 15m); QNT chops. `since` answers the price at a read's expiry."""

    def __init__(self):
        self.closes = {SYM: saw(851, amp=0.02) + RISE + [RISE[-1]], QNT: saw(864, amp=0.02),
                       BTC: [100.0, 100.3, 100.7, 101.0]}
        self.calls: list[tuple] = []

    async def fetch_ohlcv(self, sym, tf="1h", since=None, limit=60, params=None):
        self.calls.append((sym, tf, since))
        if since is not None:
            t = -(-since // M5) * M5
            return [[t, 1.02, 1.03, 1.01, 1.02, 1.0]]
        return bars(saw(limit, amp=0.03), step=H)[-limit:] if tf == "1h" else bars(self.closes[sym])[-limit:]

    async def fetch_funding_rates(self):
        return {s: {"fundingRate": 0.0001, "markPrice": c[-1]} for s, c in self.closes.items()}

    async def fetch_funding_rate_history(self, sym, limit=10):
        return [{"fundingRate": 0.0001}] * limit

    async def oi_history(self, sym, period="1h", limit=25):
        step = 4.0 / 12 if sym == SYM else 0.0
        return [{"ts": NOW - (limit - 1 - i) * M5, "oi": 100.0 + i * step, "oi_value": 1.0} for i in range(limit)]

    async def long_short_ratios(self, sym, period="1h"):
        return {"top_trader_position_ls": 0.9, "global_account_ls": 1.2}

    async def flow_klines(self, sym, interval="5m", limit=288):
        buy = 7.0 if sym == SYM else 5.0
        return [[NOW - (limit - 1 - i) * M5, 1, 1, 1, 1, 10.0, buy] for i in range(limit)]

    async def funding_intervals(self):
        return {}


def wire(g, monkeypatch, headlines=0):
    m = Market()
    for name in ("fetch_ohlcv", "fetch_funding_rates", "fetch_funding_rate_history"):
        setattr(g.ex.public, name, getattr(m, name))
    for name in ("oi_history", "long_short_ratios", "flow_klines", "funding_intervals"):
        setattr(g.ex, name, getattr(m, name))

    async def news_for(base, max_items=15):
        items = [{"title": EVIL, "url": "", "summary": "", "source": "x"}] * headlines
        return {"coin": base, "binance_announcements": [], "headlines": items, "errors": []}
    monkeypatch.setattr("shortbot.news.news_for", news_for)
    return m


def short_with_candidate(j):
    """An open short on SYM since 3h ago, QNT in the last hour's scan."""
    tid = j.create_trade(SYM, "pump_fade", "t", 1.1, 2, plan={})
    j.activate_trade(tid, 1.0, 100.0, 100.0)
    j.update_trade(tid, opened_at=iso(NOW - 3 * H))
    j.log_candidates(iso(NOW), NOW - 10 * 60_000, [{"symbol": QNT, "score": 30}], "ok")
    return tid


async def run_job(g):
    with clock.at(NOW):
        await Lane("coins", [Job("coin_memory", g.coin_memory, 0, 30)], g.n, g.ex, g.j, 0.01).tick()


def alerts(g, kind):
    return [a for a in g.j.alerts(100) if a["kind"] == kind]


# ------------------------------------------------------------ wiring
def test_the_coins_lane_runs_coin_memory_every_15_min_at_research_weight():
    assert guardian.COINS == {"coin_memory": (900, 300)}
    g = guardian_with(FakeCcxt())
    (job,) = g._lanes()["coins"].jobs
    assert job.name == "coin_memory" and job.fn == g.coin_memory and (job.every_s, job.timeout_s) == (900, 300)
    assert weight.class_for("coins", "coin_memory") == weight.RESEARCH
    assert alerts_view.is_urgent("break_watch") and not alerts_view.is_urgent("coin_event")


# ------------------------------------------------------------ one pass
async def test_a_pass_saves_states_alerts_a_real_break_and_never_closes(tmp_path, monkeypatch):
    g = guardian_on(tmp_path / "j.db")
    wire(g, monkeypatch)
    tid = short_with_candidate(g.j)
    with loop_writes(*connections(g)) as bad:
        await run_job(g)
    assert not bad, bad
    assert alerts(g, "guardian_error:coin_memory") == []
    rows = cm.states(g.j.db)
    assert set(rows) == {SYM, QNT} and rows[SYM]["focus"] == [f"trade #{tid}"] and rows[QNT]["focus"] == ["scan 30"]
    st = rows[SYM]["state"]
    assert st["px_1h_pct"] > 7 and st["oi_1h_pct"] == 4.0 and st["ti_1h"] == 0.4 and st["btc_15m_pct"] == 1.0
    assert st["break"]["score"] == 4 and st["break"]["trade_id"] == tid and st["accepted"]["side"] == "up"
    (bw,) = alerts(g, "break_watch")
    assert bw["symbol"] == SYM and f"trade #{tid}: break score 4/4" in bw["message"]
    assert "2 closes above 1.01691" in bw["message"] and "Nothing was closed" in bw["message"]
    (ev,) = alerts(g, "coin_event")                       # QNT's only event is BTC's impulse: market-wide, not alerted
    assert ev["symbol"] == SYM and "level_accepted" in ev["message"] and "btc_impulse" not in ev["message"]
    assert [e["name"] for e in rows[QNT]["state"]["events"]] == ["btc_impulse"]
    assert g.j.open_trades()[0]["id"] == tid and g.ex.fake.orders == []                # alerts never exit a trade
    await run_job(g)                                      # a second pass: nothing new, cooldowns hold
    assert len(alerts(g, "coin_event")) == 1 and len(alerts(g, "break_watch")) == 1
    assert len(cm.states(g.j.db)[SYM]["state"]["series"]) == 1   # the same 15m slot


async def test_a_pass_scores_expired_reads_and_counts_headlines_without_their_text(tmp_path, monkeypatch):
    g = guardian_on(tmp_path / "j.db")
    m = wire(g, monkeypatch, headlines=2)
    short_with_candidate(g.j)
    rid = g.j.run(cm.add_read, SYM, "fade the pop", "down", None, None, 1, 1.0, NOW - 3 * H, {})["read_id"]
    await run_job(g)
    exp = NOW - 2 * H
    assert (SYM, "5m", exp) in m.calls and cm.due_outcomes(g.j.db, NOW) == []
    assert [tuple(r) for r in g.j.db.execute("SELECT read_id, ts_ms, price_end FROM coin_read_outcomes")] == \
        [(rid, exp, 1.02)]
    assert cm.read_score(g.j.db, SYM)["directional"] == {"n": 1, "hits": 0, "hit_rate": 0.0}
    st = cm.states(g.j.db)[SYM]["state"]
    assert st["news"] == 2 and EVIL not in json.dumps(st)


async def test_one_failing_coin_is_left_out_and_a_pass_where_all_fail_is_the_jobs_failure(tmp_path, monkeypatch):
    g = guardian_on(tmp_path / "j.db")
    m = wire(g, monkeypatch)
    short_with_candidate(g.j)
    m.closes[QNT] = m.closes[QNT][:5]                     # too few bars to measure
    with clock.at(NOW):
        r = await coin_pass.refresh(g.ex, g.j, g.entries)
    assert set(r["coins"]) == {SYM} and "only 5 5m bars" in r["errors"][QNT]
    m.closes[SYM] = m.closes[SYM][:5]
    await run_job(g)
    (err,) = alerts(g, "guardian_error:coin_memory")
    assert "every coin failed" in err["message"]


async def test_the_tool_refresh_runs_the_same_pass(tmp_path, monkeypatch):
    g = guardian_on(tmp_path / "j.db")
    wire(g, monkeypatch, headlines=1)
    short_with_candidate(g.j)
    monkeypatch.setattr(cmd_desk, "get_app", lambda: g.app)
    with clock.at(NOW):
        one = await cmd_desk.coin_memory("pump", refresh=True)
    assert one["pass"] == {"coins": 1, "errors": {}, "reads_scored": 0} and one["focus"] == ["trade #1"]
    assert one["state"]["break"]["score"] == 4 and one["series"]["points"] == 1
    assert set(one["news"]["headlines"][0]["title"]) == {"untrusted_text", "source"}
    assert alerts(g, "break_watch") == []                 # the tool reports; only the guardian alerts
    with clock.at(NOW):
        allc = await cmd_desk.coin_memory(refresh=True)
    assert allc["pass"]["coins"] == 2 and [f["symbol"] for f in allc["focus"]] == [SYM, QNT]


# ------------------------------------------------------------ the noise band's fake-out p90
def test_the_fakeout_p90_comes_from_the_sweep_study_shrunk_toward_all_coins(tmp_path):
    path = tmp_path / "j.db"
    j = Journal(path)
    j.db.execute(sweeps.SCHEMA)
    rows = []
    for i in range(40):
        for sym, fake in ((SYM, 2.0), (QNT, 0.5)):
            e = {"kind": "high24h", "complete": True, "rev240": i % 2, "fakeout_mark_pct": fake, "ts": i}
            rows.append((sym, i, "high24h", 1, 1.0, json.dumps(e)))
    j.db.executemany("INSERT INTO level_sweeps VALUES (?,?,?,?,?,?)", rows)
    j.db.commit()
    t = coin_pass.fakeout_table(str(path))
    assert t["*"] == 2.0 and 0.5 < t[QNT] < 2.0 and t[SYM] == 2.0   # 40 events: w = 40/70 toward the coin's own
    assert coin_pass.fakeout_table(str(tmp_path / "none.db")) == {} and coin_pass.fakeout_table(None) == {}

"""Tiers 1-3 in the guardian's scan lane (#72): each 15m close (and fast events between closes) is evaluated, at
most 20 coins are selected, only those that fit the tier-2 weight room are deep-scored, the rest queue; every
window's trigger counts are logged; alerts are deduplicated across restarts."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

from shortbot import scan_job as SJ
from shortbot import scan_tiered as ST
from shortbot import scan_triggers as TR
from shortbot import scanner, weight
from shortbot.backtest.replay_world import logged_scans
from shortbot.journal import Journal
from shortbot.market import regime as R
from shortbot.market import streams as S
from shortbot.market.liquidations import LiquidationStore
from shortbot.notify import Notifier
from shortbot.scan_triggers import Trigger
from shortbot.signals.base import Evaluation
from tests.fakes import FakeCcxt
from tests.test_guardian import SETTINGS, guardian_with

M15 = S.TF_MS["15m"]
W0 = 1_790_000_000_000 // M15 * M15


def ccxt(raw):
    return f"{raw[:-4]}/USDT:USDT"


class Harness:
    def __init__(self, monkeypatch, universe, room=10_000.0):
        self.j = Journal(":memory:")
        self.ts = ST.TieredScan(None, self.j, Notifier(SETTINGS, self.j), Path("streams.db"), None)
        self.t = W0 + 10_000
        self.ts.now_ms = lambda: self.t
        self.universe, self.triggers, self.fast = list(universe), [], []
        self.scored, self.snaps, self.evaluated, self.deferred = [], [], 0, set()

        def snapshot(sdb, jdb, now, bars_for):
            self.snaps.append(bars_for)
            return {"universe": list(self.universe), "bars15": {}, "mark": {}, "liqs": {},
                    "ticker": {s: {"pct": 12.0, "last": 1.0, "qv": 1e7, "h": 1.1, "l": 0.9} for s in self.universe}}

        def evaluate(*a, **k):
            self.evaluated += 1
            return list(self.triggers)

        async def score(ex, journal, tickers, regime, scan, by_symbol=None, now_s=None):
            syms = [t["symbol"] for t in tickers]
            self.scored.append(syms)
            self.scan = (scan, by_symbol)
            return {"scored": [s for s in syms if s not in self.deferred], "alerts": [],
                    "deferred": [s for s in syms if s in self.deferred]}

        async def context(universe):
            return {}

        async def regime():
            return "caution"
        monkeypatch.setattr(ST, "snapshot", snapshot)
        monkeypatch.setattr(TR, "evaluate", evaluate)
        monkeypatch.setattr(TR, "fast_events", lambda *a, **k: list(self.fast))
        monkeypatch.setattr(SJ, "score_and_alert", score)
        monkeypatch.setattr(weight, "tier2_room", lambda: room)
        self.ts._context, self.ts._regime = context, regime

    def windows(self):
        cols = ("ts_ms", "kind", "coins", "counts", "selected", "scored", "queued", "skipped", "tier2_weight")
        rows = self.j.db.execute(f"SELECT {', '.join(cols)} FROM scan_windows ORDER BY rowid").fetchall()
        return [{k: (json.loads(v) if k in ("counts", "selected", "scored", "queued", "skipped") else v)
                 for k, v in zip(cols, r)} for r in rows]

    def close(self):
        self.t = (self.t // M15 + 1) * M15 + ST.CLOSE_GRACE_MS


def coins(n):
    return [f"C{i:02d}USDT" for i in range(n)]


async def test_a_close_selects_at_most_20_and_deep_scores_only_those(monkeypatch):
    h = Harness(monkeypatch, coins(30))
    h.triggers = [Trigger(s, "move_1h", 1.0 + i, "d") for i, s in enumerate(coins(30))]
    await h.ts.step()
    top = [ccxt(s) for s in reversed(coins(30))][:20]
    assert h.scored == [top] and h.snaps == [None]
    assert {c["symbol"] for c in h.ts.queued} == set(coins(10))
    w = h.windows()[0]
    assert w["kind"] == "close" and w["coins"] == 30 and w["counts"] == {"move_1h": 30}
    assert len(w["selected"]) == 20 and w["scored"] == [s.split("/")[0] + "USDT" for s in top]
    assert len(w["queued"]) == 10 and w["skipped"] == []
    scan, by = h.scan
    assert scan == {"tier": 2, "wake": "close"} and by[top[0]] == {"rules": ["move_1h"], "strength": 30.0,
                                                                   "carried": False}


async def test_the_tier2_room_caps_the_batch_and_the_rest_waits(monkeypatch):
    assert weight.TIER2_CAP == 600 and ST.EST_COIN_WEIGHT == 29     # measured: ~29 weight per deep score
    h = Harness(monkeypatch, coins(6), room=ST.WARM_WEIGHT + 3 * ST.EST_COIN_WEIGHT)
    h.triggers = [Trigger(s, "move_1h", 2.0 + i, "d") for i, s in enumerate(coins(6))]
    await h.ts.step()
    assert h.scored == [[ccxt(s) for s in ("C05USDT", "C04USDT", "C03USDT")]]
    assert [c["symbol"] for c in h.ts.queued] == ["C02USDT", "C01USDT", "C00USDT"]
    h.triggers = []
    h.close()
    await h.ts.step()                                   # carried at half strength (select): 2.0 / 2 >= 1 still kept
    assert h.scored[1] == [ccxt(s) for s in ("C02USDT", "C01USDT", "C00USDT")]


async def test_no_room_scores_nothing_and_queues_everything(monkeypatch):
    h = Harness(monkeypatch, coins(3), room=20.0)
    h.triggers = [Trigger(s, "volume_surge", 3.0, "d") for s in coins(3)]
    await h.ts.step()
    assert h.scored == [] and len(h.ts.queued) == 3 and h.windows()[0]["scored"] == []


async def test_weight_per_coin_is_measured_and_logged(monkeypatch):
    h = Harness(monkeypatch, coins(3))
    h.triggers = [Trigger(s, "move_4h", 3.0, "d") for s in coins(3)]
    usage = iter([(7, 100.0), (7, 100.0 + ST.WARM_WEIGHT + 3 * 40)])
    monkeypatch.setattr(weight, "tier2_usage", lambda: next(usage))
    await h.ts.step()
    assert h.windows()[0]["tier2_weight"] == ST.WARM_WEIGHT + 120
    assert h.ts.est == round(0.7 * ST.EST_COIN_WEIGHT + 0.3 * 40, 2)


async def test_deferred_coins_are_requeued_not_marked_scored(monkeypatch):
    h = Harness(monkeypatch, coins(2))
    h.triggers = [Trigger(s, "move_1h", 3.0, "d") for s in coins(2)]
    h.deferred = {ccxt("C01USDT")}
    await h.ts.step()
    assert [c["symbol"] for c in h.ts.queued] == ["C01USDT"] and set(h.ts.scored) == {"C00USDT"}
    assert h.windows()[0]["scored"] == ["C00USDT"]


async def test_fast_events_between_closes_score_only_the_new_coins(monkeypatch):
    h = Harness(monkeypatch, coins(3))
    await h.ts.step()                                       # the close: nothing triggered
    h.t += 30_000
    h.fast = [Trigger("C01USDT", "liq_long_burst", 2.0, "d")]
    await h.ts.step()
    assert h.scored == [[ccxt("C01USDT")]] and h.snaps == [None, ("BTCUSDT", "ETHUSDT")] and h.evaluated == 1
    h.t += 30_000
    assert await h.ts.step() is None and len(h.scored) == 1   # the same burst doesn't fire twice in a window
    assert [w["kind"] for w in h.windows()] == ["close", "fast"]


async def test_a_btc_impulse_rechecks_every_coin(monkeypatch):
    h = Harness(monkeypatch, coins(3) + ["BTCUSDT"])
    await h.ts.step()
    h.t += 30_000
    h.fast = [Trigger("BTCUSDT", "btc_impulse", 1.5, "BTCUSDT -2.1% in 15m")]
    h.triggers = [Trigger("C02USDT", "move_1h", 2.0, "d")]
    await h.ts.step()
    assert h.evaluated == 2 and h.snaps[-1] is None             # the full snapshot, every rule
    assert h.scored == [[ccxt("C02USDT")]]                      # the impulse wakes tier 1; BTC itself isn't scored
    w = h.windows()[-1]
    assert w["kind"] == "impulse" and w["counts"] == {"btc_impulse": 1, "move_1h": 1}


async def test_recently_scored_coins_skip_the_same_rules(monkeypatch):
    h = Harness(monkeypatch, coins(1))
    h.triggers = [Trigger("C00USDT", "move_4h", 2.0, "d")]
    await h.ts.step()
    h.close()
    await h.ts.step()
    assert len(h.scored) == 1 and h.windows()[1]["skipped"] == ["C00USDT"]
    h.close()
    h.triggers.append(Trigger("C00USDT", "volume_surge", 1.2, "d"))   # a new rule: rescored at once
    await h.ts.step()
    assert len(h.scored) == 2


async def test_every_close_is_logged_even_without_triggers(monkeypatch):
    h = Harness(monkeypatch, coins(4))
    await h.ts.step()
    h.close()
    await h.ts.step()
    assert [(w["kind"], w["coins"], w["counts"]) for w in h.windows()] == [("close", 4, {}), ("close", 4, {})]


async def test_stale_streams_never_score_and_alert_once(monkeypatch):
    h = Harness(monkeypatch, [])
    h.triggers = [Trigger("C00USDT", "move_1h", 9.0, "d")]
    assert await h.ts.step() is None
    h.t += ST.STALE_ALERT_S * 1000
    await h.ts.step()
    h.t += 60_000
    await h.ts.step()
    assert h.scored == [] and [a["kind"] for a in h.j.alerts(10)] == ["streams_stale"]
    h.universe = coins(1)
    await h.ts.step()
    assert h.ts.stale_since is None and len(h.scored) == 1


# ------------------------------------------------------------------ tier 2 + 3 (scan_job.score_and_alert)
def row(sym, score=40):
    return {"symbol": sym, "score": score, "setups": ["pump_fade"], "tape": {"stance": "fade", "state": "pump"},
            "vetoes": [], "change_24h": 25.0, "signals": []}


def ev(sym, deferred=()):
    return Evaluation(sym, {"last": 1.0, "data_deferred": list(deferred)}, 40, [], [], {})


async def test_score_and_alert_runs_tier2_logs_complete_rows_and_requeues_starved_ones(monkeypatch):
    seen = {}

    async def fake_eval(ex, tickers, groups=None):
        seen["cls"] = weight.current()
        return [ev("A/USDT:USDT"), ev("B/USDT:USDT", ["open_interest"])], [row("A/USDT:USDT"), row("B/USDT:USDT")]
    monkeypatch.setattr(scanner, "evaluate_tickers", fake_eval)
    j = Journal(":memory:")
    with weight.tagged(weight.RESEARCH):                      # the scan lane's class
        out = await SJ.score_and_alert(None, j, [{"symbol": "A/USDT:USDT"}, {"symbol": "B/USDT:USDT"}], "caution",
                                       {"tier": 2, "wake": "close"},
                                       {"A/USDT:USDT": {"rules": ["move_1h"], "strength": 2.0, "carried": False}},
                                       now_s=1000.0)
        await j.write(SJ.journal_batch, out)                  # written with its window (scan_tiered, #79)
    assert seen["cls"] == weight.TIER2
    assert out["scored"] == ["A/USDT:USDT"] and out["deferred"] == ["B/USDT:USDT"]
    logged = j.candidates(limit=10)
    assert [c["symbol"] for c in logged] == ["A/USDT:USDT"]
    assert logged[0]["features"]["scan"] == {"tier": 2, "wake": "close", "rules": ["move_1h"], "strength": 2.0,
                                             "carried": False}
    assert [a["symbol"] for a in out["alerts"]] == ["A/USDT:USDT"]


async def test_the_regime_check_is_research_class(monkeypatch):
    seen = []

    async def regime(ex):
        seen.append(weight.current())
        return {"verdict_for_shorts": "favorable"}
    monkeypatch.setattr(R, "market_regime", regime)
    ts = ST.TieredScan(None, Journal(":memory:"), None, Path("s.db"), None)
    assert await ts._regime() == "favorable" and await ts._regime() == "favorable"
    assert seen == [weight.RESEARCH]                          # cached for REGIME_TTL_S


async def test_alert_dedup_survives_a_restart_and_housekeeping_prunes_it(monkeypatch):
    async def fake_eval(ex, tickers, groups=None):
        return [ev("A/USDT:USDT")], [row("A/USDT:USDT")]
    monkeypatch.setattr(scanner, "evaluate_tickers", fake_eval)
    j = Journal(":memory:")
    t0 = time.time()

    async def scored(now_s):
        out = await SJ.score_and_alert(None, j, [{"symbol": "A/USDT:USDT"}], "caution", {"tier": 2}, now_s=now_s)
        await j.write(SJ.journal_batch, out)
        return out
    first, again, later = await scored(t0), await scored(t0 + 600), await scored(t0 + SJ.RENOTIFY_S + 1)
    assert len(first["alerts"]) == 1 and again["alerts"] == [] and len(later["alerts"]) == 1
    g = guardian_with(FakeCcxt())
    g.j.set(SJ.KV_SEEN + "OLD/USDT:USDT", f"{time.time() - SJ.RENOTIFY_S - 60} 40")
    g.j.set(SJ.KV_SEEN + "NEW/USDT:USDT", f"{time.time()} 40")
    await g.housekeeping()
    assert g.j.get(SJ.KV_SEEN + "OLD/USDT:USDT") is None and g.j.get(SJ.KV_SEEN + "NEW/USDT:USDT")


async def test_tickers_come_from_the_stream_with_a_research_rest_fallback():
    calls = []

    async def fetch_tickers():
        calls.append(weight.current())
        return {"B/USDT:USDT": {"symbol": "B/USDT:USDT", "percentage": -11.0, "last": 2.0, "quoteVolume": 9e6}}
    ex = SimpleNamespace(public=SimpleNamespace(fetch_tickers=fetch_tickers))
    ts = ST.TieredScan(ex, Journal(":memory:"), None, Path("s.db"), None)
    out = await ts._tickers(["A/USDT:USDT", "B/USDT:USDT"],
                            {"AUSDT": {"pct": 12.5, "last": 1.0, "qv": 2e7, "h": 1.2, "l": 0.8}})
    assert out == [{"symbol": "A/USDT:USDT", "percentage": 12.5, "last": 1.0, "quoteVolume": 2e7, "high": 1.2,
                    "low": 0.8},
                   {"symbol": "B/USDT:USDT", "percentage": -11.0, "last": 2.0, "quoteVolume": 9e6}]
    assert calls == [weight.RESEARCH]
    assert await ts._tickers(["A/USDT:USDT"], {"AUSDT": {"pct": 1, "last": 1, "qv": 1, "h": 1, "l": 1}}) and \
        len(calls) == 1                                       # all from the stream: no REST call


# ------------------------------------------------------------------ the tier-1 snapshot (no exchange calls)
def test_snapshot_reads_the_store_and_the_recorded_liquidations(tmp_path):
    now = W0 + 60_000
    st = S.StreamStore(tmp_path / "streams.db")
    rows = [[W0 - k * M15, 1.0, 1.1, 0.9, 1.0, 10.0] for k in range(130)][::-1]
    for raw, age in (("AAAUSDT", 5), ("BBBUSDT", 600)):
        st.bars(raw, "15m", rows, now)
        st.db.execute("INSERT OR REPLACE INTO fresh VALUES (?,?,?)", (raw, "kline_15m", now - age * 1000))
        st.latest("ticker", raw, {"pct": 3.0, "last": 1.0, "qv": 1e7, "h": 1, "l": 1}, now - age * 1000)
    st.flush()
    st.db.commit()
    LiquidationStore(tmp_path / "j.db").record([(now - 60_000, "AAA/USDT:USDT", "long_liquidated", 1, 1, 9e4),
                                                (now - 2 * 3_600_000, "AAA/USDT:USDT", "long_liquidated", 1, 1, 1),
                                                (now - 60_000, "BBB/USDT:USDT", "long_liquidated", 1, 1, 9e4)])
    snap = ST.snapshot(tmp_path / "streams.db", tmp_path / "j.db", now, None)
    assert snap["universe"] == ["AAAUSDT"] and list(snap["bars15"]) == ["AAAUSDT"]   # BBB's socket went quiet
    assert len(snap["bars15"]["AAAUSDT"]) == ST.BARS15 and list(snap["ticker"]) == ["AAAUSDT"]
    assert [r["notional"] for r in snap["liqs"]["AAA/USDT:USDT"]] == [9e4] and list(snap["liqs"]) == ["AAA/USDT:USDT"]
    fast = ST.snapshot(tmp_path / "streams.db", tmp_path / "j.db", now, ("BTCUSDT", "ETHUSDT"))
    assert fast["bars15"] == {} and fast["universe"] == ["AAAUSDT"]


# ------------------------------------------------------------------ replay (#29) skips tier-2 scans
def test_replay_skips_tier2_scans(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.log_candidates("t", 1000, [{"symbol": "A/USDT:USDT", "features": {"scan": {"direction": "both"}}}], "caution")
    j.log_candidates("t", 2000, [{"symbol": "B/USDT:USDT", "features": {"scan": {"tier": 2, "wake": "close"}}}],
                     "caution")
    assert [s["ts_ms"] for s in logged_scans(tmp_path / "j.db", 0, 10_000)] == [1000]


# ------------------------------------------------------------------ tier-1 context and an end-to-end window
async def test_context_has_listing_ages_delistings_unlocks_and_intervals(tmp_path):
    from shortbot.exchange import NO_DELIVERY
    from datetime import datetime, timezone

    from shortbot.market.events import EventStore
    now = int(time.time() * 1000)   # the event store is point-in-time (added_at <= now): live time here
    when = datetime.fromtimestamp(now / 1000 + 9 * 86400, timezone.utc).isoformat()
    EventStore(tmp_path / "j.db").add_unlock("CCC", when, 4.0, "team", "https://example.org/x")
    seen = []

    async def perps():
        return {"AAA/USDT:USDT": {"info": {"onboardDate": str(now - 3 * 86_400_000), "deliveryDate": NO_DELIVERY}},
                "BBB/USDT:USDT": {"info": {"onboardDate": str(now - 900 * 86_400_000), "deliveryDate": now + 1}},
                "CCC/USDT:USDT": {"info": {}}}

    async def intervals():
        seen.append(weight.current())
        return {"AAAUSDT": {"interval_h": 4, "cap": 0.02}}
    ex = SimpleNamespace(coin_perps=perps, funding_intervals=intervals)
    ts = ST.TieredScan(ex, Journal(":memory:"), None, Path("s.db"), tmp_path / "j.db")
    ts.now_ms = lambda: now
    ctx = await ts._context(["AAAUSDT", "BBBUSDT", "CCCUSDT"])
    await ts._context(["AAAUSDT"])
    assert ctx["listing_age_d"] == {"AAAUSDT": 3.0, "BBBUSDT": 900.0}
    assert ctx["events"]["BBBUSDT"] == "delisting scheduled" and ctx["events"]["CCCUSDT"].startswith("unlock in")
    assert ctx["interval_h"] == {"AAAUSDT": 4} and seen == [weight.RESEARCH]   # cached for an hour


async def test_a_real_window_from_the_store_scores_the_mover(tmp_path, monkeypatch):
    """No stubs for tier 1: bars in the store, the real snapshot, evaluate and select; only tier 2 is faked."""
    now = W0 + 10_000
    st = S.StreamStore(tmp_path / "streams.db")
    for i, raw in enumerate(("AAAUSDT", "BBBUSDT")):
        closes = [1.0 + 0.001 * ((k * 7 + i) % 5) for k in range(125)]
        if raw == "AAAUSDT":
            closes[-5:] = [1.06, 1.12, 1.18, 1.25, 1.25]                # a +25% hour on a quiet coin
        rows = [[W0 - (124 - k) * M15, c, c, c, c, 100.0] for k, c in enumerate(closes)]
        st.bars(raw, "15m", rows, now)
        st.db.execute("INSERT OR REPLACE INTO fresh VALUES (?,?,?)", (raw, "kline_15m", now - 2000))
        st.latest("ticker", raw, {"pct": 25.0 if raw == "AAAUSDT" else 0.5, "last": closes[-1], "qv": 1e7,
                                  "h": 1.3, "l": 0.9}, now - 2000)
    st.flush()
    scored = []

    async def score(ex, journal, tickers, regime, scan, by_symbol=None, now_s=None):
        scored.append((tickers, by_symbol))
        return {"scored": [t["symbol"] for t in tickers], "deferred": [], "alerts": []}

    async def context(universe):
        return {}

    async def regime():
        return "caution"
    monkeypatch.setattr(SJ, "score_and_alert", score)
    j = Journal(":memory:")
    ts = ST.TieredScan(None, j, Notifier(SETTINGS, j), tmp_path / "streams.db", tmp_path / "j.db")
    ts.now_ms, ts._context, ts._regime = (lambda: now), context, regime
    out = await ts.step()
    (tickers, by), = scored
    assert [t["symbol"] for t in tickers] == ["AAA/USDT:USDT"] and tickers[0]["percentage"] == 25.0
    assert "move_1h" in by["AAA/USDT:USDT"]["rules"] and out["coins"] == 2 and out["counts"]["move_1h"] == 1

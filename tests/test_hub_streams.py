"""DataHub reads candles from the stream store (#72) when they are fresh for the caller's weight class, else REST;
the source is recorded per timeframe; the store's readers never return rows after the market clock (#29)."""

from types import SimpleNamespace

from shortbot import clock, weight
from shortbot.config import RiskLimits
from shortbot.journal import Journal
from shortbot.market import DataHub
from shortbot.market import streams as S
from shortbot.scanner import evaluate_tickers, log_scan
from shortbot.signals import evaluate

H = S.TF_MS["1h"]
NOW = 1_790_000_000_000 // H * H + 30 * 60_000      # half past an hour
SYM, RAW = "SOL/USDT:USDT", "SOLUSDT"


def bars(n=70, now=NOW, step=H, shift=0):
    cur = now // step * step - shift * step
    return [[cur - (n - 1 - i) * step, 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, 10.0 + i] for i in range(n)]


def fill(tmp_path, rows=None, seen_age_s=1.0, now=NOW, tf="1h"):
    st = S.StreamStore(tmp_path / "streams.db")
    st.bars(SYM, tf, rows if rows is not None else bars(now=now, step=S.TF_MS[tf]), now)
    st.db.execute("INSERT OR REPLACE INTO fresh VALUES (?,?,?)", (RAW, f"kline_{tf}", int(now - seen_age_s * 1000)))
    st.db.commit()
    return st


class Public:
    def __init__(self, rows=None):
        self.rows, self.calls = rows, []

    async def fetch_ohlcv(self, symbol, tf="1h", since=None, limit=60, params=None):
        self.calls.append((symbol, tf, limit))
        return [list(r) for r in (self.rows or bars())[-limit:]]   # the bars the store holds

    async def fetch_funding_rates(self):
        return {SYM: {"fundingRate": 0.0001, "markPrice": 1.0}}


def hub(tmp_path, public=None):
    ex = SimpleNamespace(settings=SimpleNamespace(db_path=tmp_path / "shortbot.db"), public=public or Public(),
                         limits=RiskLimits())

    async def intervals():
        return {}
    ex.funding_intervals = intervals
    return DataHub(ex), ex


async def candles_at(h, cls=None, now=NOW, tf="1h", limit=60):
    ctx = h.context(SYM)
    with clock.at(now), weight.tagged(cls):
        rows = await ctx.candles(tf, limit)
    return rows, ctx


async def test_a_missing_store_reads_rest(tmp_path):
    h, ex = hub(tmp_path)
    rows, ctx = await candles_at(h, weight.RESEARCH)
    assert len(rows) == 60 and ex.public.calls == [(SYM, "1h", 60)] and ctx.sources == {"1h": "rest"}


async def test_a_fresh_store_serves_the_same_rows_without_rest(tmp_path):
    fill(tmp_path)
    h, ex = hub(tmp_path)
    rows, ctx = await candles_at(h, weight.RESEARCH)
    rest = await Public().fetch_ohlcv(SYM, "1h", limit=60)
    assert ex.public.calls == [] and ctx.sources == {"1h": "stream"}
    assert rows == rest and [type(v) for v in rows[-1]] == [type(v) for v in rest[-1]]   # same shape and values


async def test_a_stale_or_gapped_store_falls_back_to_rest(tmp_path):
    fill(tmp_path, rows=bars(shift=3))                                    # last closed bar 3h old
    h, ex = hub(tmp_path)
    assert (await candles_at(h, weight.RESEARCH))[1].sources == {"1h": "rest"} and len(ex.public.calls) == 1
    gapped = bars()
    del gapped[30]
    (tmp_path / "g").mkdir()
    fill(tmp_path / "g", rows=gapped)
    h, ex = hub(tmp_path / "g")
    assert (await candles_at(h, weight.RESEARCH))[1].sources == {"1h": "rest"} and len(ex.public.calls) == 1


async def test_a_silent_stream_falls_back_even_with_complete_bars(tmp_path):
    fill(tmp_path, seen_age_s=600)        # bars look complete but the socket went quiet 10 min ago
    h, ex = hub(tmp_path)
    assert (await candles_at(h, weight.RESEARCH))[1].sources == {"1h": "rest"} and ex.public.calls


async def test_freshness_by_weight_class_desk_needs_15s(tmp_path):
    fill(tmp_path, seen_age_s=30)
    for cls, src in ((None, "rest"), (weight.DESK, "rest"), (weight.PROTECTIVE, "rest"),
                     (weight.RESEARCH, "stream"), (weight.TIER2, "stream")):
        h, ex = hub(tmp_path)
        assert (await candles_at(h, cls))[1].sources == {"1h": src}, cls
    h, ex = hub(tmp_path)
    ctx = h.context(SYM)
    with clock.at(NOW):
        await ctx.candles("1h", 60, max_age_s=60)            # an explicit tolerance wins over the class default
    assert ctx.sources == {"1h": "stream"}


async def test_unstreamed_timeframes_never_touch_the_store(tmp_path):
    fill(tmp_path)
    h, ex = hub(tmp_path)
    _, ctx = await candles_at(h, weight.RESEARCH, tf="4h", limit=18)
    assert ex.public.calls == [(SYM, "4h", 18)] and ctx.sources == {"4h": "rest"}


async def test_a_budget_wait_is_recorded_as_deferred(tmp_path):
    h, ex = hub(tmp_path)

    async def starved(*a, **k):
        raise weight.BudgetWait("budget_wait: tier2 cap")
    ex.public.fetch_ohlcv = starved
    ctx = h.context(SYM)
    assert await ctx.candles("4h", 18) == [] and ctx.deferred == {"candles_4h"} and "candles_4h" in ctx.missing


async def test_source_is_recorded_and_the_scan_shape_is_identical(tmp_path):
    """The same bars from the store or REST: identical features apart from candle_source, logged next to `scan`."""
    (tmp_path / "rest").mkdir()
    fill(tmp_path)
    out = {}
    for name, path in (("stream", tmp_path), ("rest", tmp_path / "rest")):
        h, ex = hub(path)
        with clock.at(NOW), weight.tagged(weight.RESEARCH):
            evals, rows = await evaluate_tickers(ex, [{"symbol": SYM, "percentage": 12.0, "quoteVolume": 5e7,
                                                       "last": 70.5}], groups=("froth", "trigger"))   # no network
        j = Journal(":memory:")
        assert log_scan(j, evals, rows, "caution", {"direction": "both"}) == 1
        out[name] = (evals[0].features, rows[0], j.candidates(limit=5)[0]["features"])
    (fs, rs, ls), (fr, rr, lr) = out["stream"], out["rest"]
    assert fs["candle_source"]["1h"] == "stream" and fr["candle_source"]["1h"] == "rest"
    assert ls["candle_source"] == fs["candle_source"] and ls["scan"] == {"direction": "both"}
    strip = lambda d: {k: v for k, v in d.items() if k != "candle_source"}   # noqa: E731
    assert strip(fs) == strip(fr) and set(rs) == set(rr) and set(ls) == set(lr)
    assert fs["data_deferred"] == [] and "data_deferred" in ls


async def test_evaluate_without_a_hub_context_still_has_the_keys():
    class Bare:
        symbol, missing, features = "X/USDT:USDT", set(), {}
    ev = await evaluate(Bare(), names=("none",))
    assert ev.features["candle_source"] == {} and ev.features["data_deferred"] == []


# ------------------------------------------------------------------ readers: never after now (#29)
def test_readers_never_return_rows_written_after_now(tmp_path):
    st = fill(tmp_path)
    S.handle(st, {"stream": "!markPrice@arr@1s", "data": [{"s": RAW, "p": "1", "i": "1", "r": "0.0001", "T": 1}]},
             NOW)
    st.flush()
    p = tmp_path / "streams.db"
    assert S.candles(SYM, "1h", 60, p, NOW, max_age_s=90) is not None
    assert S.latest("mark", p, 120, NOW) and S.fresh_symbols("kline_1h", 90, p, NOW) == [RAW]
    past = NOW - 2 * H                                      # a replay two hours back: every row is from its future
    assert S.candles(SYM, "1h", 60, p, past) is None       # closed bars that close after `past`
    assert S.candles(SYM, "1h", 60, p, past, max_age_s=10 ** 6) is None
    assert S.latest("mark", p, 10 ** 6, past) == {}
    assert S.fresh_symbols("kline_1h", 10 ** 6, p, past) == []
    assert S.stale("kline_1h", 10 ** 6, p, past) == [RAW]  # a future update is unusable now: stale
    assert all(v["newest_s"] >= 0 for v in (S.freshness(p, past) or {}).values())
    with clock.at(past):                                     # the default "now" is the market clock
        assert S.candles(SYM, "1h", 60, p) is None and S.latest("mark", p, 10 ** 6) == {}


def test_path_for_and_prune_count(tmp_path):
    assert S.path_for(":memory:") is None and S.path_for("") is None and S.path_for(None) is None
    assert S.path_for(tmp_path / "shortbot.db") == tmp_path / "streams.db"
    st = fill(tmp_path, rows=bars(n=S.KEEP["1h"] + 30))
    assert st.prune(NOW) == 30 and st.prune(NOW) == 0

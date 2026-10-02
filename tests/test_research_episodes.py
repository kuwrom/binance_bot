"""#27: rescans are not independent observations. One observation per symbol-episode, a same-scan baseline,
intervals clustered by day (descriptive only below 20 days), labels that start at scan time."""

import statistics

import pytest

from shortbot.journal import Journal
from shortbot.research import label_outcomes, report
from shortbot.research_stats import clustered_mean, episodes, scan_excess, t975

H = 3_600_000
DAY = 24 * H
T0 = 1_790_000_000_000 - 1_790_000_000_000 % DAY + 2 * H     # 02:00 UTC


def _journal(rows: list[dict]) -> Journal:
    """rows: {symbol, ts_ms, ret, adv?, score?, vetoes?, features?}; one log_candidates call per scan."""
    j = Journal(":memory:")
    for ts in sorted({r["ts_ms"] for r in rows}):
        j.log_candidates("t", ts, [{"symbol": r["symbol"], "price": 1.0, "score": r.get("score", 10),
                                    "vetoes": r.get("vetoes", []), "setups": ["pump_fade"],
                                    "features": r.get("features", {})} for r in rows if r["ts_ms"] == ts], "caution")
    out = {(r["symbol"], r["ts_ms"]): r for r in rows}
    for c in j.unlabelled_candidates(older_than_ms=10**15, limit=10**6):
        r = out[(c["symbol"], c["ts_ms"])]
        j.label_candidate(c["id"], {"short_ret_24h": r["ret"], "max_adverse_24h": r.get("adv", 5.0)})
    return j


def _base(days: int = 6, n_sym: int = 8) -> list[dict]:
    return [{"symbol": f"S{i}", "ts_ms": T0 + d * DAY, "ret": (i - 3.5) * 2 + d, "adv": 20.0 if i == 0 else 3.0,
             "score": 70 if i % 2 else 10, "vetoes": ["OI $4.4M < $30M: too thin"] if i % 3 == 0 else [],
             "features": {"rsi_1h": 50 + 5 * i, "funding_rate": 0.0001, "funding_interval_h": 8}}
            for d in range(days) for i in range(n_sym)]


def _strip_rows(x):
    if isinstance(x, dict):
        return {k: _strip_rows(v) for k, v in x.items() if k != "rows"}
    return x


def test_episodes_anchor_on_first_appearance_for_the_outcome_horizon():
    rows = [{"symbol": "A", "ts_ms": 0}, {"symbol": "A", "ts_ms": 23 * H}, {"symbol": "B", "ts_ms": H},
            {"symbol": "A", "ts_ms": 24 * H}, {"symbol": "A", "ts_ms": 30 * H}, {"symbol": None, "ts_ms": 2 * H}]
    got = [(r["symbol"], r["ts_ms"] // H) for r in episodes(rows, 24)]
    assert got == [("A", 0), ("B", 1), (None, 2), ("A", 24)]
    assert len(episodes(rows, lambda r: 1.0)) == 6     # per-row horizon: 1h windows never overlap here
    assert len(episodes([{"symbol": "A"}, {"symbol": "A"}])) == 2   # no timestamp: nothing shows they overlap


def test_rescans_give_the_same_report_as_deduplicated_rows():
    base = _base()
    rescans = [{**r, "ts_ms": r["ts_ms"] + k * H, "ret": 99.0, "adv": 60.0} for r in base for k in range(1, 21)]
    a, b = report(_journal(base)), report(_journal(base + rescans))
    assert b["labelled_candidates"] == 21 * a["labelled_candidates"]
    assert a["sample"]["episodes"] == b["sample"]["episodes"] == 48 and a["sample"]["days"] == 6
    for k in ("all", "tradable", "vetoed", "by_score", "by_setup", "by_veto_reason", "by_feature_tertile",
              "by_open_interest"):
        assert _strip_rows(a[k]) == _strip_rows(b[k]), k
    assert b["all"]["rows"] == 21 * a["all"]["rows"] and b["all_rows"]["n"] == 21 * 48   # legacy block: inflated


def test_day_clustering_widens_the_interval_and_the_scan_baseline_removes_the_day_shock():
    shock = [12.0 if d % 2 else -9.0 for d in range(25)]
    rows = [{"symbol": f"S{i}", "ts_ms": T0 + d * DAY, "ret": shock[d] + 0.3 * i}
            for d in range(25) for i in range(10)]
    r = report(_journal(rows))["all"]
    vals = [x["ret"] for x in rows]
    iid_half = 1.96 * statistics.stdev(vals) / len(vals) ** 0.5
    lo, hi = r["ci95_short_ret_24h"]
    assert r["n"] == 250 and r["days"] == 25 and "descriptive_only" not in r
    assert (hi - lo) / 2 > 2.5 * iid_half and r["n_eff"] < 50
    elo, ehi = r["ci95_excess_ret_24h"]
    assert ehi - elo < (hi - lo) / 10          # same-scan median absorbs the shared move


def test_few_days_are_labelled_descriptive_only_and_one_day_has_no_interval():
    two = report(_journal(_base(days=2)))
    assert "descriptive only" in two["inference"] and two["all"]["descriptive_only"] is True
    assert two["basis"] == "episodes (since 2026-10-01)"
    one = report(_journal(_base(days=1)))["all"]
    assert one["days"] == 1 and one["ci95_short_ret_24h"] is None and one["n_eff"] is None


def test_scan_excess_is_ret_minus_same_scan_median_over_distinct_symbols():
    scan = [{"symbol": s, "ts_ms": 1, "short_ret_24h": v} for s, v in zip("ABCDE", (1.0, 2.0, 3.0, 4.0, 10.0))]
    dup = {"symbol": "E", "ts_ms": 1, "short_ret_24h": 10.0}
    small = [{"symbol": s, "ts_ms": 2, "short_ret_24h": 1.0} for s in "ABCD"]
    assert scan_excess(scan + [dup] + small) == [-2.0, -1.0, 0.0, 1.0, 7.0, 7.0, None, None, None, None]


def test_costs_subtract_round_trip_and_add_estimated_funding_carry():
    r = report(_journal(_base(days=2)))
    a = r["all"]
    # funding 0.01% per 8h, held 24h: +0.03% to a short; fees+slippage 0.1% round trip
    assert a["avg_net_ret_24h"] == pytest.approx(a["avg_short_ret_24h"] - 0.07, abs=0.011)
    assert r["costs"]["round_trip_pct"] == 0.1 and r["baseline"]["kind"] == "same_scan_median"


def test_feature_mining_is_reported_as_exploratory_and_veto_keys_drop_numbers():
    rows = _base(days=3)
    rows[1]["vetoes"] = ["OI $8.1M < $30M: too thin"]
    r = report(_journal(rows), min_n=1)
    fm = r["feature_mining"]
    assert fm["features"] == len(r["by_feature_tertile"]) >= 1 and fm["buckets"] >= 3
    assert "exploratory" in fm["note"]
    assert list(r["by_veto_reason"]) == ["OI $#M < $#M"]
    buckets = [b for t in r["by_feature_tertile"].values() for b in t.values() if b["n"]]
    assert buckets and all(b["descriptive_only"] is True for b in buckets)     # 3 days: the flag survives compact
    assert "several buckets" in r["how_to_read"]


def test_t_tail_and_quantile_match_the_tables():
    from shortbot.research_stats import t_ppf, t_sf
    assert t_sf(2.093, 19) == pytest.approx(0.025, abs=2e-5) and t_sf(12.706, 1) == pytest.approx(0.025, abs=2e-5)
    assert t_sf(0.0, 7) == 0.5 and t_sf(-2.093, 19) == pytest.approx(0.975, abs=2e-5)
    assert t_sf(3.0, 19) == pytest.approx(0.003685, abs=2e-5)
    assert t_ppf(0.975, 19) == pytest.approx(2.093, abs=1e-3) and t_ppf(0.975, 2000) == pytest.approx(1.9612, abs=1e-3)
    assert t_ppf(0.025, 19) == pytest.approx(-2.093, abs=1e-3)
    c = clustered_mean([1.0, 2.0, 3.0, 5.0], ["a", "a", "b", "b"])
    assert c["se"] == pytest.approx((c["ci95"][1] - c["mean"]) / t975(1))


def test_report_reads_more_than_5000_rows():
    rows = [{"symbol": f"S{k % 100}", "ts_ms": T0 + (k // 100) * 30 * 60_000, "ret": 1.0} for k in range(5200)]
    assert report(_journal(rows))["labelled_candidates"] == 5200


def test_clustered_mean_and_t_quantiles():
    assert t975(1) == 12.706 and t975(30) == 2.042 and t975(1000) == pytest.approx(1.962, abs=1e-3)
    c = clustered_mean([1.0, 2.0, 3.0, 4.0], ["d1", "d1", "d2", "d2"])
    assert c["mean"] == 2.5 and c["days"] == 2 and c["ci95"][0] < 2.5 < c["ci95"][1]
    assert clustered_mean([1.0, 2.0], ["d", "d"])["ci95"] is None


class _Pub:
    def __init__(self, spike_ms: int):
        self.calls, self.spike_ms = [], spike_ms

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append((symbol, timeframe, since, limit))
        step = {"5m": 300_000, "1h": H}[timeframe]
        return [[since + i * step, 1.0, 1.5 if since + i * step <= self.spike_ms < since + (i + 1) * step else 1.01,
                 0.98, 0.99 if i < 287 else 0.9, 1.0] for i in range(limit)]


class _Ex:
    def __init__(self, spike_ms: int):
        self.public = _Pub(spike_ms)


async def test_labels_start_at_scan_time_not_the_floored_hour():
    j = Journal(":memory:")
    ts = T0 + 31 * 60_000 + 10_000                       # 02:31:10
    j.log_candidates("t", ts, [{"symbol": "X/USDT:USDT", "price": 1.0, "features": {}}], None)
    ex = _Ex(spike_ms=T0 + 30 * 60_000 + 30_000)          # 02:30:30 spike: before the scan, same hour and 5m bar
    assert await label_outcomes(ex, j, now_ms=ts + 100 * H) == 1
    assert ex.public.calls == [("X/USDT:USDT", "5m", T0 + 35 * 60_000, 864)]
    c = j.candidates(labelled_only=True)[0]
    assert c["max_adverse_24h"] == pytest.approx(1.0) and c["short_ret_24h"] == pytest.approx(10.0)
    assert c["short_ret_4h"] == pytest.approx(1.0) and c["short_ret_72h"] == pytest.approx(10.0)
    assert c["label_v"] == 2 and j.unlabelled_candidates(older_than_ms=10**15) == []


def test_legacy_labels_are_queued_for_relabelling():
    j = _journal(_base(days=1, n_sym=3))
    assert j.unlabelled_candidates(older_than_ms=10**15) == []
    j.db.execute("UPDATE candidates SET label_v=NULL WHERE symbol='S1'")
    assert [r["symbol"] for r in j.unlabelled_candidates(older_than_ms=10**15)] == ["S1"]
    assert report(j)["sample"]["legacy_labels"] == 1

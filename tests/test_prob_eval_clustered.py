"""#27 in the calibration report: overlapping research brackets count once per (source, symbol) episode, and
P(target first) intervals are clustered by day (Wilson with the df-adjusted effective n)."""

from shortbot.probability.evaluate import brier, expectancy
from shortbot.research_stats import clustered_wilson, wilson

H = 3_600_000
DAY = 24 * H
T0 = 1_790_000_000_000 - 1_790_000_000_000 % DAY + 2 * H


def _rows(days: int, symbols: int, rescans: int, hit_every: int, p_model: float = 0.10) -> list[dict]:
    """One scan a day; each symbol rescanned `rescans` times 30 min apart (24h brackets overlap)."""
    out = []
    for d in range(days):
        for s in range(symbols):
            hit = (d * symbols + s) % hit_every == 0
            for k in range(rescans):
                out.append({"source": "scan", "symbol": f"S{s}", "ts_ms": T0 + d * DAY + k * 30 * 60_000,
                            "hours": 24, "entry": 1.0, "stop": 1.05, "target": 0.90, "p_model": p_model,
                            "outcome": "target" if hit else "stop", "_y": hit, "context": {"setup": "a"}})
    return out


def test_overlapping_rows_count_once_and_two_days_are_not_significant():
    rows = _rows(days=2, symbols=50, rescans=10, hit_every=50)
    k = sum(r["_y"] for r in rows)
    assert wilson(k, len(rows))[1] < 0.10            # the old row-based CI would call this a real (negative) edge
    e = expectancy(rows, lambda r: r["context"]["setup"])["a"]
    assert e["rows"] == 1000 and e["n"] == 100 and e["days"] == 2 and e["descriptive_only"] is True
    assert e["ci95"][1] > 0.10 and e["edge_significant"] is False


def test_many_days_with_a_real_edge_stay_significant():
    e = expectancy(_rows(days=30, symbols=10, rescans=4, hit_every=2, p_model=0.333),
                   lambda r: r["context"]["setup"])["a"]
    assert e["n"] == 300 and e["days"] == 30 and "descriptive_only" not in e
    assert e["p_target_observed"] == 0.5 and e["edge_significant"] is True


def test_clustered_wilson_needs_two_days_and_shrinks_n_with_day_correlation():
    assert clustered_wilson([1, 0, 1], ["d", "d", "d"])["ci95"] is None
    ys = [1] * 20 + [0] * 20 + [1, 0] * 10                    # day1 all hits, day2 none, day3 half
    days = ["d1"] * 20 + ["d2"] * 20 + ["d3"] * 20
    c = clustered_wilson(ys, days)
    lo, hi = c["ci95"]
    ilo, ihi = wilson(sum(ys), len(ys))
    assert c["n_eff"] < 10 and hi - lo > 2 * (ihi - ilo)


def test_reliability_diagram_intervals_are_clustered_by_day():
    rows = [{"p": 0.3, "_y": d == 0, "ts_ms": T0 + d * DAY + i * 60_000} for d in range(3) for i in range(30)]
    b = brier(rows, "p")["reliability_diagram"][0]
    assert b["n"] == 90 and b["days"] == 3
    assert b["ci"][1] - b["ci"][0] > 2 * (wilson(30, 90)[1] - wilson(30, 90)[0])

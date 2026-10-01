import numpy as np
import pytest

from shortbot.backtest import carry_regime as C

DAY = C.DAY
D0 = 1_600_000_000_000 // DAY * DAY


def prints(days: int, rate: float, start: int = D0, per_day: int = 3) -> list[tuple]:
    step = DAY // per_day
    return [(start + i * step, rate) for i in range(days * per_day)]


def test_daily_funding_sums_the_days_prints_whatever_the_interval():
    f = C.daily_funding(prints(2, 0.0001) + prints(1, 0.0002, start=D0 + 2 * DAY, per_day=24))
    assert f[D0] == pytest.approx(0.0003) and f[D0 + 2 * DAY] == pytest.approx(0.0048)


def test_carry_is_annualised_and_needs_a_full_window_on_both_majors():
    funding = {"BTCUSDT": prints(10, 0.0001), "ETHUSDT": prints(10, 0.0003)}
    c = C.carry_series(funding)
    assert min(c) == D0 + (C.CARRY_DAYS - 1) * DAY          # first day with 7 full days
    assert c[D0 + 9 * DAY] == pytest.approx((0.0003 + 0.0009) / 2 * 365)
    assert C.carry_series({"BTCUSDT": prints(10, 0.0001)}) == {}


def test_percentiles_use_only_past_days():
    series = {D0 + i * DAY: float(i % 17) for i in range(400)}
    p1 = C.percentiles(series)
    changed = dict(series)
    changed[D0 + 399 * DAY] = 1e9                              # the future must not move earlier ranks
    p2 = C.percentiles(changed)
    assert all(p1[d] == p2[d] for d in p1 if d < D0 + 399 * DAY)
    assert min(p1) == D0 + C.PCT_MIN * DAY


def test_short_return_includes_funding_received_and_delivery_exit():
    cl = {D0: (100.0, 1e7), D0 + DAY: (95.0, 1e7)}            # last bar day 1, then delisted
    assert C._exit_close(cl, D0, 7, D0 + DAY) == 95.0
    assert C._exit_close(cl, D0, 1, D0 + DAY) == 95.0
    assert C._exit_close(cl, D0 + DAY, 7, D0 + DAY) is None   # no bar after entry
    assert C._exit_close(cl, D0, 7, D0 + DAY, end=D0 + DAY) is None   # end of the data is not a delisting
    fund = {D0 + DAY: 0.001, D0 + 2 * DAY: 0.001, D0 + 9 * DAY: 0.5}
    assert C._short_ret(100.0, 90.0, fund, D0, 3) == pytest.approx(0.10 + 0.002)


def test_nw_diff_matches_mean_difference_and_needs_both_groups():
    y = np.array([1.0, 2.0, 3.0, 10.0, 11.0, 12.0])
    d = np.array([0, 0, 0, 1, 1, 1], dtype=bool)
    diff, t = C.nw_diff(y, d, 0)
    assert diff == pytest.approx(9.0) and t > 5
    assert C.nw_diff(y, np.zeros(6, dtype=bool), 2)[1] is None


def _market(n_days: int = 60) -> dict:
    """BTC/ETH flat; 12 alts falling 1%/day; ALT0 jumps +30% on day 40 then halves."""
    bars, funding = {}, {}
    for s in ("BTCUSDT", "ETHUSDT", *(f"ALT{i}USDT" for i in range(12))):
        px, rows = 100.0, []
        for i in range(n_days):
            d = D0 + i * DAY
            if s.startswith("ALT"):
                px *= 1.30 if (s == "ALT0USDT" and i == 40) else 0.5 if (s == "ALT0USDT" and i == 41) else 0.99
            rows.append((d, px, px, px, px, 1e7))
        bars[s], funding[s] = rows, prints(n_days, 0.0)
    return {"bars": bars, "funding": funding}


def test_outcomes_alt_basket_and_gainer_fade():
    data = _market()
    days = [D0 + i * DAY for i in range(31, 50)]
    out = C.outcomes(data, days)
    assert out["alt_short_7d"][D0 + 31 * DAY] == pytest.approx(1 - 0.99 ** 7)       # window ends before the jump
    assert out["fade_short_1d"] == {D0 + 40 * DAY: pytest.approx(0.5)}
    assert D0 + 31 * DAY not in out["alt_short_30d"]           # window runs past the data
    assert out["btc_short_7d"][D0 + 35 * DAY] == pytest.approx(0.0)


def test_evaluate_flags_a_regime_that_predicts_the_outcome():
    days = [D0 + i * DAY for i in range(800)]
    rng = np.random.default_rng(0)
    pct = {d: float(rng.random()) for d in days}
    carry = {d: 0.1 for d in days}
    ser = {d: (0.05 if pct[d] >= 0.9 else 0.0) + float(rng.normal(0, 0.01)) for d in days}
    outs = {k: ({} if k != "alt_short_7d" else ser) for k in C.OUTCOMES}
    rows = {(r["regime"], r["outcome"]): r for r in C.evaluate(pct, carry, outs)}
    top = rows[("top_decile", "alt_short_7d")]
    assert top["diff"] == pytest.approx(0.05, abs=0.01) and top["verdict"] == "GATE"
    assert rows[("bottom_quintile", "alt_short_7d")]["diff"] < 0

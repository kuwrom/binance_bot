"""Round-1 backtest logic: the pre-registered fixes (strategies_v2)."""

import pytest

from shortbot.backtest import strategies_v2 as v2
from shortbot.backtest.features import Market
from shortbot.backtest.sim import Plan
from shortbot.backtest.strategies import trials
from tests.test_backtest import T0, _synthetic, _truncate

H = 3_600_000
SAT = 1790380800000                   # 2026-09-26 00:00 UTC (a Saturday)


def _coin(sym="AAA/USDT:USDT", n=60, tags=("Meme",), btc_ramp=0.0, prem=0.0, funding=None, taker=None, t0=T0):
    perp = []
    for k in range(n):
        px = 100.0 * (1 + btc_ramp) ** k
        perp.append({"ts": t0 + k * H, "o": px, "h": px * 1.01, "l": px * 0.99, "c": px, "qv": 1e6, "n": 1,
                     "tbq": 0.5e6})
    return {"symbol": sym, "onboard_ms": 1, "tags": list(tags), "perp": perp, "oi": {}, "spot": [],
            "premium": {t0 + k * H: prem for k in range(n)},
            "funding": funding or [[t0 + k * H, 0.0001] for k in range(0, n, 8)],
            "taker_ratio": taker or {}}


def test_tide_veto_blocks_entries_while_btc_squeezes():
    calm = Market([_coin("BTC/USDT:USDT", n=100, tags=("PoW",)), _coin(n=100)])
    hot = Market([_coin("BTC/USDT:USDT", n=100, tags=("PoW",), btc_ramp=0.001), _coin(n=100)])  # +2.4% / 24h
    assert v2.tide_ok(calm, calm.coins["AAA/USDT:USDT"], 99)
    assert not v2.tide_ok(hot, hot.coins["AAA/USDT:USDT"], 99)


def test_taker_veto_uses_the_hourly_ratio_or_the_4h_imbalance():
    raw = _coin(n=100)
    m = Market([raw])
    c = m.coins["AAA/USDT:USDT"]
    assert v2.taker_ok(m, c, 99) and v2.taker_imbalance(c.rows, 99) == pytest.approx(0.0)
    for r in c.rows[96:100]:
        r["tbq"] = 0.6e6                                  # 60% buys -> imbalance +0.2
    assert not v2.taker_ok(m, c, 99)
    raw2 = _coin(n=100, taker={T0 + 100 * H: 1.2})        # hourly ratio stamped at the bar's close
    m2 = Market([raw2])
    assert not v2.taker_ok(m2, m2.coins["AAA/USDT:USDT"], 99)


def test_discount_needs_a_perp_discount_negative_funding_or_a_1h_funding_interval():
    m = Market([_coin(n=100)])
    assert not v2.discount(m, m.coins["AAA/USDT:USDT"], 99)
    m = Market([_coin(n=100, prem=-0.012)])
    assert v2.discount(m, m.coins["AAA/USDT:USDT"], 99)
    hourly = [[T0 + k * H, 0.0001] for k in range(0, 100, 8)] + [[T0 + 97 * H, 0.0], [T0 + 98 * H, 0.0]]
    m = Market([_coin(n=100, funding=sorted(hourly))])
    c = m.coins["AAA/USDT:USDT"]
    assert v2.funding_interval_h(c.funding, T0 + 100 * H) == 1.0 and v2.discount(m, c, 99)
    assert v2.funding_interval_h(c.funding, T0 + 90 * H) == 8.0


def test_weekend_is_judged_at_the_signal_bar_close():
    m = Market([_coin(n=100, t0=SAT - 50 * H)])
    c = m.coins["AAA/USDT:USDT"]
    assert v2.weekend(m, c, 49) and not v2.weekend(m, c, 48)   # bar 49 closes at Sat 00:00


def test_stop_floor_widens_the_stop_and_keeps_target_prices():
    m = Market([_coin(n=100)])
    c = m.coins["AAA/USDT:USDT"]
    atr = v2.atr_abs(c, 99)
    pl = Plan(stop=101.0, targets=((99.0, 0.5), (98.0, 0.5)), time_stop=12, be_r=1.0,
              meta={"stop_atr": 1 / atr, "targets_r": ((1.0, 0.5), (2.0, 0.5))})
    w = v2.floor_stop(pl, c, 99)
    assert w.stop == pytest.approx(100.0 + 3 * atr) and w.targets == pl.targets
    assert w.meta["stop_atr"] == pytest.approx(3.0)
    assert w.meta["targets_r"][0][0] == pytest.approx(1.0 / (3 * atr))
    far = Plan(stop=110.0, targets=pl.targets, time_stop=12, meta={})
    assert v2.floor_stop(far, c, 99) is far


def test_round1_ledger_adds_17_named_trials_on_top_of_round_0():
    names = [t.name for t in v2.trials_v2()]
    assert len(names) == 17 == len(set(names))
    assert len(v2.ledger()) == len(trials()) + 17


def test_variants_are_subsets_of_their_base_and_do_not_look_ahead():
    coins = _synthetic()
    full, cut_i = Market(coins), 135
    cut = Market(_truncate(coins, T0 + cut_i * H))
    for t in v2.trials_v2():
        base_fn = v2.BASE[t.name.split("+")[0]][0]
        for s, c in full.coins.items():
            got = {i for i, _ in t.fn(full, c, t.params)}
            assert got <= {i for i, _ in base_fn(full, c, t.params)}, t.name
            a = {(i, p.stop) for i, p in t.fn(full, c, t.params) if i <= cut_i}
            b = {(i, p.stop) for i, p in t.fn(cut, cut.coins[s], t.params)}
            assert a == b, t.name

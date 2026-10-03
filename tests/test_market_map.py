import asyncio
import math

from shortbot.market.market_map import (beta, coin_row, log_returns, market_header, market_vetoes, rotation_flags,
                                        sector_table)
from shortbot.market.sectors import base_of, primary_sector
from shortbot.signals.sector import sector


def candles(closes, vol=100.0):
    return [[i * 3_600_000, c, c, c, c, vol] for i, c in enumerate(closes)]


BTC = [100 * math.exp(0.01 * math.sin(i / 3)) for i in range(170)]
BTC_R = log_returns(BTC)


def test_taxonomy():
    assert primary_sector(["Infrastructure", "RWA"]) == "RWA"
    assert primary_sector(["Layer1_Layer2", "pos"]) == "L1/L2"
    assert primary_sector(["Seed", "pos"]) == "other"
    assert base_of("1000PEPE/USDT:USDT") == "PEPE" and base_of("1INCH/USDT:USDT") == "1INCH"


def test_beta_recovers_leverage_and_shrinks():
    coin = [2 * r for r in BTC_R]
    assert abs(beta(coin, BTC_R) - (0.7 * 2 + 0.3)) < 1e-6
    assert beta([0.0] * 5, BTC_R) == 1.0          # too little data -> neutral


def test_residual_strips_btc_beta():
    closes = [c * 1.0 for c in BTC]
    closes[-1] *= 1.05                             # +5% idiosyncratic jump on top of BTC
    row = coin_row("X/USDT:USDT", candles(closes), BTC_R, {"1h": 0.0, "4h": 0.0, "24h": 0.0})
    assert row["r1h"] > 4 and row["resid1h"] > 4


def rows_for(n, r4, above, sector_resid=0.0):
    return [{"symbol": f"C{i}/USDT:USDT", "r1h": 0.1, "r4h": r4, "r24h": r4, "resid1h": 0.1, "resid4h": r4,
             "resid24h": sector_resid + i, "resid_vol_1h": 1.0, "above_ema20": i < above, "vol_surge": 2.0,
             "rets": []} for i in range(n)]


def test_sector_table_and_vetoes():
    rows = rows_for(6, 4.0, 5)
    for r in rows:
        r["symbol"] = "A" + r["symbol"]
    sectors = {r["symbol"].split("/")[0]: "AI" for r in rows}
    table = sector_table(rows, sectors)
    assert table[0]["sector"] == "AI" and table[0]["breadth_pct"] == 83 and table[0]["leader"] == "AC5"
    rotation_flags(table, [])
    header = market_header(rows, {"1h": 2.0, "4h": 1.0}, {"1h": 0.0})
    v = market_vetoes(header, table, 1.0, 0.1)
    assert "rallying" in v["sectors"]["AI"]["vetoes"][0]
    assert any("squeeze" in p for p in v["pause"]) and any("BTC +1.00%" in p for p in v["pause"])


def test_rotation_needs_a_climb_held_for_two_maps():
    table = [{"sector": s, "rank4h": i + 1, "breadth_pct": 70, "vol_surge": 2.0} for i, s in enumerate("ABCDEFGH")]
    rotation_flags(table, [{"A": 7}, {"A": 2}])
    assert table[0]["rotation_in"]
    rotation_flags(table, [{"A": 2}, {"A": 2}])
    assert not table[0]["rotation_in"]            # was never in the bottom half


def test_sector_signal_applies_vetoes_and_laggard_points():
    m = {"ts": 1, "header": {"regime": "RISK-OFF", "breadth_pct": 20},
         "coins": {"X/USDT:USDT": {"sector": "AI", "resid_sector_z": -1.5, "beta": 1.2}},
         "vetoes": {"pause": [], "sectors": {"AI": {"vetoes": ["AI sector rallying"], "leader": "Y", "rank4h": 1,
                                                    "resid24h": -2.0, "rotation_out": False}}}}

    class Ctx:
        symbol = "X/USDT:USDT"

        async def market_map(self):
            return m

    out = asyncio.run(sector(Ctx()))
    assert out.vetoes == ["AI sector rallying"] and out.points and out.features["sector"] == "AI"


def test_coin_row_survives_missing_btc_returns():
    from shortbot.market.market_map import coin_row
    c1h = [[i, 1, 1.01, 0.99, 1 + 0.001 * (i % 5), 100] for i in range(60)]
    row = coin_row("X/USDT:USDT", c1h, [], {"1h": None, "4h": None, "24h": None})
    assert row["resid_vol_1h"] is None

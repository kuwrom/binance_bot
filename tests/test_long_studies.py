"""backtest/long_studies.py on a synthetic daily panel: each event rule fires on its case, excess nets out the
benchmark, the trend gate times BTC, and the register rows carry the pre-registered variants."""

from shortbot.backtest import long_studies as L
from shortbot.backtest.carry_regime import DAY
from shortbot.backtest.xs_factors import Panel

D0 = 1_600_000_000_000 // DAY * DAY
N = 160


def panel(paths: dict[str, list[float]], funding: dict[str, dict[int, float]] | None = None) -> Panel:
    bars = {s: [(D0 + k * DAY, c, c * 1.01, c * 0.99, c, 50e6) for k, c in enumerate(cl)] for s, cl in paths.items()}
    fund = {s: [(d, r) for d, r in sorted(f.items())] for s, f in (funding or {}).items()}
    return Panel({"bars": bars, "funding": fund})


def flat(n=N, px=100.0):
    return [px] * n


def test_breakout_fires_once_and_measures_excess():
    paths = {f"C{i}USDT": flat() for i in range(8)} | {L.BTC: flat()}
    paths["UPUSDT"] = flat(100) + [110.0 * 1.02 ** k for k in range(N - 100)]   # breaks out at day 100, keeps going
    st = L.Study(panel(paths))
    ev = L.breakout(st)["hold5"]
    assert [(e["s"], (e["d"] - D0) // DAY) for e in ev] == [("UPUSDT", 100), ("UPUSDT", 120), ("UPUSDT", 140)]
    e = ev[0]
    bench = st.bench(e["d"], 5)
    assert abs(e["excess"] - (e["net"] - bench)) < 1e-12 and e["net"] > 0


def test_tsmom_is_flat_when_btc_trends_down():
    down = [100 * 0.99 ** k for k in range(N)]
    st = L.Study(panel({L.BTC: down} | {f"C{i}USDT": flat() for i in range(5)}))
    res = L.tsmom(st)["btc|r20"]
    assert res["timed"]["exposure"] == 0.0 and res["timed"]["max_dd"] == 0.0
    assert res["buy_hold"]["max_dd"] < -0.4


def test_capitulation_needs_a_coin_specific_drop():
    paths = {f"C{i}USDT": flat() for i in range(10)} | {L.BTC: flat()}
    paths["CRASHUSDT"] = flat(100) + [60.0] * (N - 100)                        # -40% at day 100, alone
    ev = L.capitulation(L.Study(panel(paths)))["hold7"]
    assert [(e["s"], (e["d"] - D0) // DAY) for e in ev] == [("CRASHUSDT", 100)]


def test_alt_negative_funding_holds_until_z_recovers():
    f = {D0 + k * DAY: 0.0001 * (1 + (k % 3)) for k in range(N)}
    f[D0 + 100 * DAY] = -0.002                                                   # z << -1.5 on day 100
    paths = {f"C{i}USDT": flat() for i in range(6)} | {L.BTC: flat(), "NEGUSDT": flat()}
    ev = L.alt_negative_funding(L.Study(panel(paths, {"NEGUSDT": f})))
    assert [(e["s"], (e["d"] - D0) // DAY) for e in ev] == [("NEGUSDT", 100)]


def test_rows_cover_every_preregistered_variant():
    res = {"tsmom": {f"{a}|{g}": {"timed": {"days": 10, "mean_daily": 0.001, "t": 1.0}} for a in ("btc", "basket")
                     for g in L.GATES} | {"weekly_basket|r60": {}},
           "breakout_20d": {k: {"n": 3} for k in ("hold5", "hold10", "hold20", "chandelier3")},
           "residual_momentum": {k: {"n": 3} for k in ("lb7", "lb14", "lb28")},
           "alt_negative_funding": {"z-1.5": {"n": 3}},
           "capitulation": {k: {"n": 3} for k in ("hold3", "hold7", "hold14")}}
    rows = L.rows(res)
    assert len(rows) == 19 and len({r["name"] for r in rows}) == 19
    assert all(r["family"].startswith("lib.") for r in rows)

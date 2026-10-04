"""The remaining library cards our own data can test (task #28). Research only; everything fixed before the run.

Daily (histdaily, 2020-2026, BTC unless stated). Returns are long-side, funding paid included:
- negative_funding_streak_long: BTC's 30-day mean funding has been < 0 for >= 30 consecutive days and the close is
  above its 20DMA -> long BTC, held 30d and 60d. One entry per streak.
- funding_negative_zscore_long: the 30-day z-score of BTC daily funding crosses below -1.5 -> long until z > 0 or
  30 days. Non-overlapping.
- dominance_rotation_long: weekly (Sunday close); regime on = ETH/BTC above its 50DMA and BTC's 14d return below
  the top-20 alt basket's -> long the 5 alts with the best 14d return residual to BTC (beta from 60d), held 7d.
  Compared with the same pick on regime-off weeks and with the top-20 basket.
- cycle_low_accumulation: BTC > 20% under its 200DMA, 30-day mean funding < 0 and > 40% below its all-time high ->
  long BTC, held 90d and 180d. One entry per 90 days.
Every daily test is compared with unconditional BTC (or basket) returns over the same holding length.

Hourly (hist.db, the 60-day window):
- funding_settlement_drift: around each funding print with |rate| >= 0.1%, the return of the hour into the print and
  of the 2 hours after, signed so positive = the payer side loses (short when funding > 0), versus the same coins'
  average hour.
- intraday_hour_seasonality: mean return by UTC hour (all coins and BTC), day-clustered t; first vs second half.
- btc_alt_lead_lag: alts' next-hour return after a BTC hour of >= +1% / <= -1%, versus all hours.

    .venv/bin/python -m shortbot.backtest.card_tests [--hourly]
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import cluster_se as C
from .. import provenance as P
from .carry_regime import DAY, STABLES, daily_funding


def _t(xs: list[float]) -> float | None:
    if len(xs) < 3 or statistics.stdev(xs) == 0:
        return None
    return round(statistics.fmean(xs) / (statistics.stdev(xs) / math.sqrt(len(xs))), 2)


def _ct(xs: list[float], keys: list[Any]) -> float | None:
    t, g = C.one_way_t(xs, keys)
    return round(t, 2) if t is not None and g >= 3 else None


def _clustered(pairs: list[tuple[int, float]]) -> dict[str, Any]:
    """Pooled mean of (hour ts, x) pairs with ITS t clustered by hour (coins moving in the same hour are one event)
    and by UTC day (#31: CV3 on the pooled mean; before #31 the t was over equal-weighted hour means)."""
    xs, keys = [x for _, x in pairs], [k for k, _ in pairs]
    return {"n": len(pairs), "clusters": len(set(keys)), "mean": round(statistics.fmean(xs), 5) if pairs else None,
            "t_clustered": _ct(xs, keys), "t_day": _ct(xs, [k // DAY for k in keys])}


def _summ(xs: list[float], base: list[float] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(xs), "mean": round(statistics.fmean(xs), 4) if xs else None, "t": _t(xs)}
    if base:
        out["baseline_mean"] = round(statistics.fmean(base), 4)
        out["excess"] = round(out["mean"] - out["baseline_mean"], 4) if xs else None
    return out


def _sma(cl: list[float], i: int, n: int) -> float | None:
    return sum(cl[i - n + 1:i + 1]) / n if i >= n - 1 else None


def btc_series(data: dict[str, Any]) -> tuple[list[int], list[float], list[float]]:
    bars = data["bars"]["BTCUSDT"]
    days, closes = [b[0] for b in bars], [b[4] for b in bars]
    f = daily_funding(data["funding"].get("BTCUSDT", []))
    return days, closes, [f.get(d, 0.0) for d in days]


def hold_ret(closes: list[float], fund: list[float], i: int, h: int) -> float | None:
    """Long return close i -> close i+h, minus the funding paid."""
    if i + h >= len(closes):
        return None
    return closes[i + h] / closes[i] - 1 - sum(fund[i + 1:i + h + 1])


def negative_funding_streak(days, closes, fund) -> dict[str, Any]:
    m30 = [statistics.fmean(fund[i - 29:i + 1]) if i >= 29 else None for i in range(len(fund))]
    out: dict[int, list[float]] = {30: [], 60: []}   # hold days -> returns
    streak, used = 0, False
    events = []
    for i, m in enumerate(m30):
        streak = streak + 1 if m is not None and m < 0 else 0
        if streak == 0:
            used = False
        s20 = _sma(closes, i, 20)
        if streak >= 30 and not used and s20 and closes[i] > s20:
            used = True
            events.append(datetime.fromtimestamp(days[i] / 1000, UTC).date().isoformat())
            for h in (30, 60):
                r = hold_ret(closes, fund, i, h)
                if r is not None:
                    out[h].append(r)
    base = {h: [r for i in range(len(closes)) if (r := hold_ret(closes, fund, i, h)) is not None] for h in (30, 60)}
    return {"events": events, **{f"{h}d": _summ(out[h], base[h]) for h in (30, 60)}}


def funding_zscore_long(days, closes, fund) -> dict[str, Any]:
    rets, events, i = [], [], 30
    while i < len(fund) - 1:
        w = fund[i - 30:i]
        sd = statistics.pstdev(w)
        z = (fund[i] - statistics.fmean(w)) / sd if sd else 0.0
        if z <= -1.5:
            j = i + 1
            while j < min(len(fund) - 1, i + 30):
                w2 = fund[j - 30:j]
                sd2 = statistics.pstdev(w2)
                if sd2 and (fund[j] - statistics.fmean(w2)) / sd2 > 0:
                    break
                j += 1
            r = hold_ret(closes, fund, i, j - i)
            if r is not None:
                rets.append(r)
                events.append((datetime.fromtimestamp(days[i] / 1000, UTC).date().isoformat(), j - i))
            i = j + 1
            continue
        i += 1
    mean_h = round(statistics.fmean(h for _, h in events)) if events else 5
    base = [r for k in range(len(closes)) if (r := hold_ret(closes, fund, k, mean_h)) is not None]
    return {"n_events": len(events), "avg_hold_d": mean_h, **_summ(rets, base)}


def cycle_low(days, closes, fund) -> dict[str, Any]:
    out: dict[int, list[float]] = {90: [], 180: []}   # hold days -> returns
    last, ath = -10 ** 9, 0.0
    events = []
    for i, c in enumerate(closes):
        ath = max(ath, c)
        s200 = _sma(closes, i, 200)
        if s200 is None or i < 30 or i - last < 90:
            continue
        if c < 0.8 * s200 and statistics.fmean(fund[i - 29:i + 1]) < 0 and c < 0.6 * ath:
            last = i
            events.append(datetime.fromtimestamp(days[i] / 1000, UTC).date().isoformat())
            for h in (90, 180):
                r = hold_ret(closes, fund, i, h)
                if r is not None:
                    out[h].append(r)
    base = {h: [r for i in range(len(closes)) if (r := hold_ret(closes, fund, i, h)) is not None] for h in (90, 180)}
    return {"events": events, **{f"{h}d": _summ(out[h], base[h]) for h in (90, 180)}}


def dominance_rotation(data: dict[str, Any]) -> dict[str, Any]:
    bars = {s: {b[0]: b for b in rows} for s, rows in data["bars"].items() if s not in STABLES}
    btc, eth = bars["BTCUSDT"], bars.get("ETHUSDT", {})
    days = sorted(btc)
    on: list[float] = []
    off: list[float] = []
    basket_on: list[float] = []
    basket_off: list[float] = []
    for i, d in enumerate(days):
        if datetime.fromtimestamp(d / 1000, UTC).weekday() != 6 or i < 60 or i + 7 >= len(days):
            continue
        d14, d7, d60 = days[i - 14], days[i + 7], days[i - 60]
        ratio = [eth[x][4] / btc[x][4] for x in days[i - 49:i + 1] if x in eth]
        if len(ratio) < 50:
            continue
        alts = [s for s, b in bars.items() if s not in ("BTCUSDT", "ETHUSDT") and d in b and d14 in b and d7 in b
                and d60 in b]
        alts = sorted(alts, key=lambda s: -sum(bars[s][x][5] for x in days[i - 29:i + 1] if x in bars[s]))[:20]
        if len(alts) < 10:
            continue
        r14 = {s: bars[s][d][4] / bars[s][d14][4] - 1 for s in alts}
        b14 = btc[d][4] / btc[d14][4] - 1
        regime = ratio[-1] > statistics.fmean(ratio) and b14 < statistics.fmean(r14.values())
        beta = {}
        for s in alts:
            xs = [btc[days[k]][4] / btc[days[k - 1]][4] - 1 for k in range(i - 59, i + 1)]
            ys = [bars[s][days[k]][4] / bars[s][days[k - 1]][4] - 1 if days[k] in bars[s] and days[k - 1] in bars[s]
                  else 0.0 for k in range(i - 59, i + 1)]
            vx = statistics.pvariance(xs)
            beta[s] = (sum((a - statistics.fmean(xs)) * (b - statistics.fmean(ys)) for a, b in zip(xs, ys, strict=True))
                       / len(xs) / vx) if vx else 1.0
        resid = {s: r14[s] - beta[s] * b14 for s in alts}
        top = sorted(alts, key=lambda s: -resid[s])[:5]
        fwd = statistics.fmean(bars[s][d7][4] / bars[s][d][4] - 1 for s in top)
        bask = statistics.fmean(bars[s][d7][4] / bars[s][d][4] - 1 for s in alts)
        (on if regime else off).append(fwd)
        (basket_on if regime else basket_off).append(bask)
    return {"regime_on_top5": _summ(on, basket_on), "regime_off_top5": _summ(off, basket_off),
            "on_minus_off": round(statistics.fmean(on) - statistics.fmean(off), 4) if on and off else None}


def run_daily(db: Path) -> dict[str, Any]:
    from ..histdaily import load
    data = load(str(db))
    days, closes, fund = btc_series(data)
    return {"negative_funding_streak_long": negative_funding_streak(days, closes, fund),
            "funding_negative_zscore_long": funding_zscore_long(days, closes, fund),
            "cycle_low_accumulation": cycle_low(days, closes, fund),
            "dominance_rotation_long": dominance_rotation(data)}


def run_hourly() -> dict[str, Any]:
    from .features import Market
    from .run import load_coins
    m = Market(load_coins(True))
    coins = [c for c in m.coins.values() if len(c.rows) > 200]
    mid = (m.t0 + m.t1) // 2
    # settlement drift: payer-side loss = -sign(rate) * return
    pre, post, base = [], [], []
    by_hour: dict[int, list[tuple[str, float]]] = {}
    for c in coins:
        rows = c.rows
        rets = [rows[k]["c"] / rows[k]["o"] - 1 for k in range(len(rows))]
        base.extend(rets)
        for k, r in enumerate(rows):
            by_hour.setdefault(datetime.fromtimestamp(r["ts"] / 1000, UTC).hour, []).append(
                (datetime.fromtimestamp(r["ts"] / 1000, UTC).date().isoformat(), rets[k]))
        for k, rate in c.fidx.items():          # fidx: bar whose OPEN is the print time
            if abs(rate) < 0.001 or k < 1 or k + 2 >= len(rows):
                continue
            sgn = 1 if rate > 0 else -1
            pre.append((rows[k]["ts"], -sgn * rets[k - 1]))                      # the hour INTO the print
            post.append((rows[k]["ts"], -sgn * (rows[k + 1]["c"] / rows[k]["o"] - 1)))   # the 2 hours after
    hours = {}
    for h, xs in sorted(by_hour.items()):
        hours[h] = {"n": len(xs), "days": len({d for d, _ in xs}),
                    "mean_bp": round(statistics.fmean(r for _, r in xs) * 1e4, 2),
                    "t_day": _ct([r for _, r in xs], [d for d, _ in xs])}
    halves = {}
    for half, cond in (("first", lambda ts: ts < mid), ("second", lambda ts: ts >= mid)):
        hm: dict[int, list[float]] = {}   # UTC hour -> bar returns
        for c in coins:
            for r in c.rows:
                if cond(r["ts"]):
                    hm.setdefault(datetime.fromtimestamp(r["ts"] / 1000, UTC).hour, []).append(r["c"] / r["o"] - 1)
        halves[half] = {h: statistics.fmean(v) for h, v in hm.items()}
    common = sorted(set(halves["first"]) & set(halves["second"]))
    stability = statistics.correlation([halves["first"][h] for h in common], [halves["second"][h] for h in common])
    btc = m.btc
    lead_up, lead_dn, lead_all = [], [], []
    if btc:
        bret = {r["ts"]: r["c"] / r["o"] - 1 for r in btc.rows}
        for c in coins:
            if c is btc:
                continue
            for k in range(1, len(c.rows)):
                b = bret.get(c.rows[k - 1]["ts"])
                if b is None:
                    continue
                nxt = c.rows[k]["c"] / c.rows[k]["o"] - 1
                lead_all.append(nxt)
                if b >= 0.01:
                    lead_up.append((c.rows[k]["ts"], nxt))
                elif b <= -0.01:
                    lead_dn.append((c.rows[k]["ts"], nxt))
    return {"funding_settlement_drift": {"hour_into_print_payer_loss": _clustered(pre),
                                         "two_hours_after_payer_loss": _clustered(post),
                                         "avg_hour_bp": round(statistics.fmean(base) * 1e4, 2)},
            "intraday_hour_seasonality": {"by_hour": hours, "half_to_half_corr": round(stability, 3)},
            "btc_alt_lead_lag": {"after_btc_up_1pct": _clustered(lead_up), "after_btc_down_1pct": _clustered(lead_dn),
                                 "all_hours_mean": round(statistics.fmean(lead_all), 5) if lead_all else None}}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="shortbot.backtest.card_tests")
    ap.add_argument("--hourly", action="store_true")
    ap.add_argument("--db", type=Path)
    ap.add_argument("--no-save", action="store_true")
    given = list(sys.argv[1:] if argv is None else argv)
    a = ap.parse_args(given)
    if a.hourly:
        res, fp = run_hourly(), {}
    else:
        db = a.db or P.data_dir() / "daily.db"
        res, fp = run_daily(db), {"daily_db": P.fingerprint(db)}
    prov = out = None
    if not a.no_save:
        prov = P.stamp(P.config("shortbot.backtest.card_tests", given), fp)
        print("saved to", out := P.write_json("card_tests_hourly" if a.hourly else "card_tests_daily", prov, res, None))
    print(json.dumps(res, indent=1, default=str))
    from .. import trial_register as TR
    from .register_rows import card_rows
    print(f"trial register: {TR.record('card_tests', card_rows(res), prov, out)} rows")


if __name__ == "__main__":
    main()

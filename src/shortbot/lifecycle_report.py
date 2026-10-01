"""Tables for the listing-lifecycle study (docs/LIFECYCLE_STUDY.md): loads data/lifecycle.db (lifecycle_data.py),
computes per-coin metrics (lifecycle_study.py), attaches features (listing type, tags, float, FDV/MC, listing size,
spot status, listing quarter) and prints markdown tables.

    PYTHONPATH=src .venv/bin/python -m shortbot.lifecycle_report > /tmp/tables.md
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from . import lifecycle_study as ls
from .lifecycle_data import DAY_MS, LIFE_DB

TYPE_ORDER = ("HODLer", "Launchpool", "Megadrop", "PreMarket", "Alpha", "SpotNew", "PerpOnly", "SpotOld")


def _flags(base: str, titles: list[tuple[int, str]]) -> set[str]:
    tick = re.escape(base)
    pat = re.compile(rf"\({tick}\)|\b{tick}USDT\b|\b1000{tick}USDT\b")
    out = set()
    for cat, t in titles:
        if cat != 48 or not pat.search(t):
            continue
        for key, word in (("HODLer", "HODLer Airdrops"), ("Launchpool", "Launchpool"), ("Megadrop", "Megadrop"),
                          ("PreMarket", "Pre-Market"), ("Alpha", "Binance Alpha"), ("Seed", "Seed Tag")):
            if word in t:
                out.add(key)
    return out


def _supply(s: dict | None) -> tuple[float | None, float | None]:
    if not s:
        return None, None
    denom, circ = s.get("max_supply") or s.get("total_supply"), s.get("circulating_supply")
    mc, fdv = s.get("market_cap"), s.get("fully_diluted_valuation")
    return (min(circ / denom, 1.0) if circ and denom else None), (fdv / mc if fdv and mc else None)


def load(path=LIFE_DB) -> list[dict[str, Any]]:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    btc = {r[0]: (r[1], r[2]) for r in db.execute("SELECT ts, o, c FROM klines WHERE symbol='BTCUSDT'")}
    titles = db.execute("SELECT catalog, title FROM announcements").fetchall()
    spot = dict(db.execute("SELECT base, first_ms FROM spot").fetchall())
    sup = {b: json.loads(d) for b, d in db.execute("SELECT base, data FROM supply")}
    coins = []
    for sym, base, onboard, status, delivery, subtypes in db.execute(
            "SELECT symbol, base, onboard_ms, status, delivery_ms, subtypes FROM coins"):
        bars = db.execute("SELECT ts, o, h, l, c, qv FROM klines WHERE symbol=? ORDER BY ts", (sym,)).fetchall()
        if len(bars) < 8:
            continue
        complete = status != "TRADING"
        m = ls.coin_metrics(bars, btc, complete)
        tags = set(json.loads(subtypes)) - {"Crypto", "USDC"}
        flags = _flags(base, titles) | ({"Alpha"} if "Alpha" in tags else set())
        s_ms = spot.get(base)
        gap = (s_ms - bars[0][0]) / DAY_MS if s_ms else None
        spot_cls = ("perp_only" if gap is None else "spot_old" if gap < -30
                    else "perp_first" if gap > 1 else "spot_same")
        ltype = next((t for t in TYPE_ORDER[:5] if t in flags), None) or (
            {"perp_only": "PerpOnly", "spot_old": "SpotOld"}.get(spot_cls, "SpotNew"))
        fl, fdv_mc = _supply(sup.get(base))
        d0 = datetime.fromtimestamp(bars[0][0] / 1000, UTC)
        coins.append({**m, "symbol": sym, "base": base, "tags": tags, "flags": flags, "ltype": ltype,
                      "spot": spot_cls, "float": fl, "fdv_mc": fdv_mc, "month": d0.strftime("%Y-%m"),
                      "quarter": f"{d0.year}Q{(d0.month - 1) // 3 + 1}", "delisted": complete,
                      "life_days": (min(delivery, bars[-1][0] + DAY_MS) - bars[0][0]) / DAY_MS if complete else None})
    return coins


# ------------------------------------------------------------------ formatting
def pct(x, d=0):
    return "–" if x is None else f"{x * 100:+.{d}f}%"


def share(x):
    return "–" if x is None else f"{x * 100:.0f}%"


def num(x, d=0):
    return "–" if x is None else f"{x:.{d}f}"


def table(head: list[str], rows: list[list]) -> str:
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
                     + ["| " + " | ".join(map(str, r)) + " |" for r in rows])


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def frac(xs, fn):
    xs = [x for x in xs if x is not None]
    return sum(map(fn, xs)) / len(xs) if xs else None


def support(coins, key) -> str:
    groups = defaultdict(list)
    for c in coins:
        groups[c["quarter"]].append(c.get(key))
    ok, n_q = ls.consistent(groups)
    n = sum(c.get(key) is not None for c in coins)
    tag = "**well-supported**" if n >= 30 and n_q >= 3 and ok >= n_q - 1 and ok / max(n_q, 1) >= 0.75 else "anecdote"
    return f"{tag} (n={n}, same sign {ok}/{n_q} qtrs)"


# ------------------------------------------------------------------ sections
def horizons(coins) -> str:
    rows = []
    for h in ls.HORIZONS:
        r, x = [c[f"r_open_{h}"] for c in coins], [c[f"x_open_{h}"] for c in coins]
        rows.append([f"{h}d", sum(v is not None for v in r), pct(med(r)), pct(ls.quantile(r, .25)),
                     pct(ls.quantile(r, .75)), share(frac(r, lambda v: v < 0)), pct(med(x)),
                     share(frac(x, lambda v: v < 0)), pct(med([c[f"r_w1h_{h}"] for c in coins])),
                     pct(med([c[f"r_ath_{h}"] for c in coins]))])
    return table(["horizon", "n", "median vs open", "p25", "p75", "% below listing", "median minus BTC",
                  "% below (BTC-adj)", "median vs week-1 high", "median h days after ATH"], rows)


def drawdowns(coins) -> str:
    rows = []
    for ref in ("peak", "open"):
        for lv in ls.DRAWDOWNS:
            d = [c[f"dd_{ref}_{int(lv * 100)}"] for c in coins]
            rows.append([f"−{int(lv * 100)}% from {'running peak' if ref == 'peak' else 'listing open'}",
                         num(ls.km_median(d)), share(ls.km_by(d, 30)), share(ls.km_by(d, 90)),
                         share(ls.km_by(d, 180)), sum(e for _, e in d)])
    return table(["drawdown", "KM median days", "by day 30", "by day 90", "by day 180", "coins that got there"],
                 rows)


def pump(coins) -> str:
    buckets = (("day 0", 0, 0), ("day 1-3", 1, 3), ("day 4-7", 4, 7), ("day 8-30", 8, 30), ("> day 30", 31, 10**6))
    rows = [[b, share(frac([c["ath_day"] for c in coins], lambda d, lo=lo, hi=hi: lo <= d <= hi))]
            for b, lo, hi in buckets]
    rows += [["median ATH vs open", pct(med([c["ath_vs_open"] for c in coins]))],
             ["median week-1 high vs open", pct(med([c["w1_high_vs_open"] for c in coins]))],
             ["median +50% bounces after ATH", num(med([c["bounces"] for c in coins]), 1)],
             ["% with ≥ 1 +50% bounce after ATH", share(frac([c["bounces"] for c in coins], lambda b: b >= 1))],
             ["median volume day 30 / week 1", share(med([c["vol_30"] for c in coins]))],
             ["median volume day 90 / week 1", share(med([c["vol_90"] for c in coins]))]]
    return table(["pump window / after-life", "value"], rows)


def delisted(coins) -> str:
    d = [c for c in coins if c["delisted"]]
    life = [c["life_days"] for c in d]
    return table(["delisted perps", "n", "median days listed", "min", "max", "median return open→last"],
                 [["all", len(d), num(med(life)), num(min(life, default=None)), num(max(life, default=None)),
                   pct(med([c["r_open_180"] for c in d]))]])


def _km50(cs):
    return num(ls.km_median([c["dd_peak_50"] for c in cs]))


def split(coins, name, keyfn) -> str:
    groups = defaultdict(list)
    for c in coins:
        for k in keyfn(c):
            groups[k].append(c)
    rows = []
    for k, cs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        rows.append([k, len(cs), pct(med([c["r_open_30"] for c in cs])), pct(med([c["r_open_90"] for c in cs])),
                     pct(med([c["x_open_90"] for c in cs])), share(frac([c["r_open_90"] for c in cs], lambda v: v < 0)),
                     _km50(cs), pct(med([c["ath_vs_open"] for c in cs])),
                     pct(ls.trade_stats([c["trades"]["d7"][60] for c in cs]).get("med_ret"))])
    return f"**{name}**\n\n" + table(["group", "n", "med 30d", "med 90d", "90d minus BTC", "% below @90d",
                                      "KM days to −50% (peak)", "med ATH vs open", "short d7→60d med"], rows)


def fmt(v: float) -> str:
    return f"{v / 1e6:.0f}M" if v >= 1e6 else f"{v:.2f}"


def tertile(coins, key):
    vals = sorted(c[key] for c in coins if c[key] is not None)
    if len(vals) < 9:
        return lambda c: []
    cut = (ls.quantile(vals, 1 / 3), ls.quantile(vals, 2 / 3))

    def f(c):
        v = c[key]
        if v is None:
            return ["unknown"]
        a, b = fmt(cut[0]), fmt(cut[1])
        i = 0 if v <= cut[0] else 1 if v <= cut[1] else 2
        return [(f"low (≤ {a})", f"mid ({a}–{b})", f"high (> {b})")[i]]
    return f


def shorts(coins) -> str:
    rows = []
    for entry in list(coins[0]["trades"]):
        for n in ls.HOLDS:
            s = ls.trade_stats([c["trades"][entry][n] for c in coins])
            if s["n"] < 10:
                continue
            rows.append([entry, n, s["n"], pct(s["med_ret"]), share(s["win"]), pct(s["med_mae"]), pct(s["p90_mae"]),
                         share(s["mae50"]), share(s["mae100"]), pct(s["mean"], 1), pct(s["mean_stop"], 1),
                         pct(s["mean_stop50"], 1)])
    return table(["entry (close of)", "hold d", "n", "median short ret", "win %", "median MAE", "p90 MAE",
                  "MAE ≥ 50%", "MAE ≥ 100%", "mean (no stop)", "mean w/ 30% stop", "mean w/ 50% stop"], rows)


def shorts_by_quarter(coins, cells) -> str:
    qs = sorted({c["quarter"] for c in coins})
    rows = []
    for e, n in cells:
        row = [f"{e} → {n}d"]
        for q in qs:
            s = ls.trade_stats([c["trades"][e][n] for c in coins if c["quarter"] == q])
            row.append(f"{pct(s['mean_stop'], 0)} (n={s['n']})" if s["n"] >= 5 else "–")
        rows.append(row)
    return table(["mean w/ 30% stop", *qs], rows)


def short_support(coins, entry, n) -> str:
    for c in coins:
        t = c["trades"][entry][n]
        c["_t"] = t["ret"] if t else None
    return support(coins, "_t")


def shorts_by(coins, name, keyfn, cell=("d7", 60)) -> str:
    groups = defaultdict(list)
    for c in coins:
        for k in keyfn(c):
            groups[k].append(c["trades"][cell[0]][cell[1]])
    rows = []
    for k, ts in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        s = ls.trade_stats(ts)
        if s["n"]:
            rows.append([k, s["n"], pct(s["med_ret"]), share(s["win"]), share(s["mae100"]), pct(s["mean"], 1),
                         pct(s["mean_stop"], 1), pct(s["mean_stop50"], 1)])
    return f"**{name}: short {cell[0]} → {cell[1]}d**\n\n" + table(
        ["group", "n", "median", "win %", "MAE ≥ 100%", "mean (no stop)", "mean 30% stop", "mean 50% stop"], rows)


def predictors(coins) -> str:
    feats = {"log first-week volume": lambda c: math.log10(c["w1_qv"]) if c["w1_qv"] > 0 else None,
             "float % (circ/max)": lambda c: c["float"], "FDV / MC": lambda c: c["fdv_mc"],
             "week-1 high vs open": lambda c: c["w1_high_vs_open"], "day-7 close vs open": lambda c: c["r_open_7"],
             "ATH vs open (HINDSIGHT)": lambda c: c["ath_vs_open"], "days to ATH (HINDSIGHT)": lambda c: c["ath_day"], "perp-only (no spot)": lambda c: float(c["spot"] == "perp_only"),
             "old coin (spot > 30d before)": lambda c: float(c["spot"] == "spot_old"),
             "Alpha": lambda c: float("Alpha" in c["flags"]), "Meme tag": lambda c: float("Meme" in c["tags"]),
             "AI tag": lambda c: float("AI" in c["tags"]), "BTC 30d over the same window": lambda c: c["btc_30"]}
    outs = {"90d ret vs open": lambda c: c["r_open_90"], "90d minus BTC": lambda c: c["x_open_90"],
            "short d7→60d": lambda c: (c["trades"]["d7"][60] or {}).get("ret"),
            "d7→60d MAE ≥ 100%": lambda c: (float(c["trades"]["d7"][60]["mae"] >= 1)
                                            if c["trades"]["d7"][60] else None),
            "−50% from peak within 60d": lambda c: (float(c["dd_peak_50"][1] and c["dd_peak_50"][0] <= 60)
                                                    if c["dd_peak_50"][1] or c["age"] > 60 else None)}
    rows = []
    for fn, f in feats.items():
        row = [fn]
        for f_out in outs.values():
            r, n = ls.spearman([f(c) for c in coins], [f_out(c) for c in coins])
            row.append(f"{num(r, 2)} (n={n})")
        rows.append(row)
    return table(["feature (Spearman ρ)", *outs], rows)


def report(coins) -> str:
    new = [c for c in coins if c["spot"] != "spot_old"]
    q = defaultdict(int)
    for c in coins:
        q[c["quarter"]] += 1
    parts = [f"coins: {len(coins)} (new tokens {len(new)}); delisted {sum(c['delisted'] for c in coins)}; "
             f"by quarter {dict(sorted(q.items()))}",
             "## Horizons: all\n" + horizons(coins), "## Horizons: new tokens (excl. old coins)\n" + horizons(new),
             "support 30d: " + support(coins, "r_open_30") + "; 90d: " + support(coins, "r_open_90")
             + "; 90d minus BTC: " + support(coins, "x_open_90") + "; 180d: " + support(coins, "r_open_180"),
             "## Drawdowns (Kaplan-Meier)\n" + drawdowns(coins), "## Pump window\n" + pump(coins),
             "## Delisted\n" + delisted(coins),
             "## Splits\n" + "\n\n".join([
                 split(coins, "listing type", lambda c: [c["ltype"]]),
                 split(coins, "product tag", lambda c: sorted(c["tags"] & {"Meme", "AI", "Layer-1", "Layer-2", "DeFi",
                                                                            "Infrastructure", "Gaming", "Alpha"}) or ["untagged"]),
                 split(coins, "spot status", lambda c: [c["spot"]]),
                 split(coins, "float (circ/max)", tertile(coins, "float")),
                 split(coins, "FDV / MC", tertile(coins, "fdv_mc")),
                 split(coins, "first-week quote volume (USD)", tertile(coins, "w1_qv")),
                 split(coins, "listing quarter", lambda c: [c["quarter"]]),
                 split(coins, "BTC over the coin's first 30d", lambda c: ["BTC up" if (c["btc_30"] or 0) > 0 else "BTC down"])]),
             "## Short entries (all coins)\n" + shorts(coins),
             "## Short entries (new tokens)\n" + shorts(new),
             "## Predictors\n" + predictors(coins)]
    best = [("d7", 60), ("d7", 30), ("d14", 60), ("w1_low_break", 30), ("peak_-30%", 30), ("d0", 30), ("d1", 30)]
    parts.append("## Short entries by group\n" + "\n\n".join([
        shorts_by(coins, "listing type", lambda c: [c["ltype"]]),
        shorts_by(coins, "spot status", lambda c: [c["spot"]]),
        shorts_by(coins, "first-week volume", tertile(coins, "w1_qv")),
        shorts_by(coins, "float", tertile(coins, "float")),
        shorts_by(coins, "week-1 high vs open", tertile(coins, "w1_high_vs_open")),
        shorts_by(coins, "BTC over the first 30d", lambda c: ["BTC up" if (c["btc_30"] or 0) > 0 else "BTC down"])]))
    parts.append("## Short stability by quarter\n" + shorts_by_quarter(coins, best))
    parts.append("## Short support by quarter\n" + "\n".join(f"- {e} hold {n}: {short_support(coins, e, n)}" for e, n in best))
    return "\n\n".join(parts)


if __name__ == "__main__":
    print(report(load()))

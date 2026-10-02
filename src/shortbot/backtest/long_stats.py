"""Statistics for the long studies (long_studies.py): series stats, two-group tests and the event summary with its
tail diagnostics. Both daily long edges were found to live in the tail (median excess < 0), so every event summary
reports the median, the 5%-trimmed mean, the top 5%'s share of the total and a 4-week block bootstrap of the mean."""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from .. import cluster_se as C
from .carry_regime import DAY

BOOT_BLOCK_DAYS, BOOT_N, BOOT_SEED = 28, 2000, 7


def _day(d: int) -> str:
    return datetime.fromtimestamp(d / 1000, UTC).date().isoformat()


def tails(xs: list[float]) -> dict[str, Any]:
    s = sorted(xs)
    k = len(s) // 20
    total = math.fsum(s)
    return {"median": round(statistics.median(s), 4),
            "trimmed5": round(statistics.fmean(s[k:len(s) - k]), 4) if len(s) > 2 * k else None,
            "top5_share": round(math.fsum(s[-k:]) / total, 2) if k and total else None}


def block_bootstrap(events: list[dict[str, Any]], key: str = "excess") -> dict[str, Any]:
    """Resample BOOT_BLOCK_DAYS blocks of entry days (with all their events): the mean's 95% interval and P(mean <= 0)."""
    blocks: dict[int, list[float]] = defaultdict(list)
    for e in events:
        blocks[e["d"] // (BOOT_BLOCK_DAYS * DAY)].append(e[key])
    keys, rnd = list(blocks), random.Random(BOOT_SEED)
    means = sorted(statistics.fmean([x for k in rnd.choices(keys, k=len(keys)) for x in blocks[k]])
                   for _ in range(BOOT_N))
    return {"blocks": len(keys), "ci95": [round(means[int(0.025 * BOOT_N)], 4), round(means[int(0.975 * BOOT_N)], 4)],
            "p_le_0": round(sum(m <= 0 for m in means) / BOOT_N, 4)}


def _maxdd(rets: list[float]) -> float:
    eq = peak = 1.0
    dd = 0.0
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
    return round(dd, 3)


def _series_stats(rets: list[float], exposure: float | None = None) -> dict[str, Any]:
    mu, sd = statistics.fmean(rets), statistics.stdev(rets)
    return {"days": len(rets), "mean_daily": round(mu, 5),
            "t": round(mu / (sd / math.sqrt(len(rets))), 2) if sd else None,
            "sharpe_ann": round(mu / sd * math.sqrt(365), 2) if sd else None, "max_dd": _maxdd(rets),
            "total": round(math.prod(1 + r for r in rets) - 1, 2),
            **({"exposure": round(exposure, 2)} if exposure is not None else {})}


def _two_groups(a: list[float], b: list[float]) -> dict[str, Any]:
    if len(a) < 3 or len(b) < 3:
        return {"n_on": len(a), "n_off": len(b)}
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    return {"n_on": len(a), "n_off": len(b), "on": round(statistics.fmean(a), 4), "off": round(statistics.fmean(b), 4),
            "diff": round(statistics.fmean(a) - statistics.fmean(b), 4),
            "t_welch": round((statistics.fmean(a) - statistics.fmean(b)) / se, 2) if se else None}


def summarise(events: list[dict[str, Any]], block_days: int) -> dict[str, Any]:
    """events: {d, s, net, excess, gate}. Pooled excess with the two-way (time block x coin) t, by year and gate."""
    if len(events) < 5:
        return {"n": len(events)}
    blk = max(block_days, 7) * DAY
    xs = [e["excess"] for e in events]
    inf = C.mean_inference(xs, [e["d"] // blk for e in events], [e["s"] for e in events])
    yrs: dict[int, list[float]] = {}
    for e in events:
        yrs.setdefault(datetime.fromtimestamp(e["d"] / 1000, UTC).year, []).append(e["excess"])
    by_gate = {}
    for on in (True, False):
        sub = [e for e in events if e["gate"] is on]
        if len(sub) >= 5:
            i2 = C.mean_inference([e["excess"] for e in sub], [e["d"] // blk for e in sub], [e["s"] for e in sub])
            by_gate["on" if on else "off"] = {"n": len(sub), "excess": round(i2["mean"], 4),
                                              "t_2way": _r(i2["t_2way"])}
    return {"n": len(events), "coins": inf["coins"], "blocks": inf["days"],
            "net": round(statistics.fmean(e["net"] for e in events), 4),
            "win": round(sum(e["net"] > 0 for e in events) / len(events), 2),
            "excess": round(inf["mean"], 4), "se": inf["se_2way"], "t_2way": _r(inf["t_2way"]),
            "t_block": _r(inf["t_day"]), "t_coin": _r(inf["t_coin"]), "n_eff": _r(inf["n_eff"], 0),
            "by_year": {y: round(statistics.fmean(v), 4) for y, v in sorted(yrs.items())},
            "by_gate": by_gate, "tails": tails(xs), "bootstrap": block_bootstrap(events), "daily": _daily(events)}


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def _daily(events: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for e in events:
        out[_day(e["d"])] = out.get(_day(e["d"]), 0.0) + e["excess"]
    return out

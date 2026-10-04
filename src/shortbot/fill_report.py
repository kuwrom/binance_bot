"""The fill_quality section (#35, in journal stats): each fill recorded by fill_quality.py against its mainnet
reference, next to what backtest/sim.py assumes for the same fill; and the hook that calibrates that model from
them. Read-only, no network.

- bps per fill, positive = worse for the short. ref = the mainnet book mid, else the last print, else the mark.
  entry (a SELL): (ref - px) / ref; exit (a BUY): (px - ref) / ref. bps_mark: the same against the mark.
- stale: the reference is more than MAX_LAG_MS from the fill (a thin coin's last print well before it): listed,
  kept out of every aggregate and of the calibration.
- model: sim.Costs.slip(24h quote volume) x the sim's charge for that fill (Costs.charge): a market entry or a desk
  close 1x (sim: entry at the close, time stop), a limit 0 (fills at its price as maker), a stop 2x (stop_slip_mult;
  `exchange` exits, the stop-outs found in trade history, are charged as stops). Volume: fill_refs.vol24_usd (mainnet
  24 h ticker at the first entry), else entry_features.quote_volume (the scan's); sim sums the 24 hourly bars to the
  signal bar. sim's clip (1-150 bp) applies before the multiplier, as there.
- by_fill: per leg/type, 95% intervals clustered by UTC day (research_stats). Per trade (qty-weighted), by setup and
  by entry type. Descriptive only below research_stats.MIN_DAYS days; n < 30 trades is an anecdote.
- calibrate(): k in slip = k / sqrt(vol $M) from market and stop fills: least squares through the origin on
  x = mult / sqrt(vol $M), and the median of bps / x (robust to one squeeze fill). ok only with MIN_TRADES trades over
  MIN_DAYS days. Demo fills calibrate the DEMO venue (thinner book): live needs live fills (the journal's mode).
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from dataclasses import replace
from typing import Any

from . import research_stats as rs
from .backtest.sim import COSTS, Costs
from .journal_tables import NOT_DRILL

MAX_LAG_MS = 5_000
MIN_TRADES = 30
RECENT = 20
# a desk fill -> the sim fill it is charged as (sim.Costs.charge): a desk close is a market order (sim's time exit), a
# stop-out found in trade history a stop, a resting target a target
SIM_KIND = {"entry/market": "entry/market", "entry/limit": "entry/limit", "entry/stop": "entry/stop",
            "exit/market": "exit/time", "exit/exchange": "exit/stop", "exit/stop": "exit/stop",
            "exit/target": "exit/target", "exit/limit": "exit/target"}
HOW = ("bps of the fill price vs the mainnet reference at the fill (book mid, else last print, else mark); positive "
       "= worse for the short. model_bps = what backtest/sim.py charges the same fill: 20 bp / sqrt(24h vol $M), "
       "clipped 1-150 bp, 2x on stops, 0 on limits. excess = actual - model. Demo fills vs mainnet prices: they "
       "measure the demo venue, not what live would cost.")


def _pos(x: Any) -> float | None:
    try:
        return float(x) if x is not None and float(x) > 0 else None
    except (TypeError, ValueError):
        return None


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def ref_price(f: dict[str, Any]) -> float | None:
    bid, ask = _pos(f.get("bid")), _pos(f.get("ask"))
    return (bid + ask) / 2 if bid and ask else _pos(f.get("last")) or _pos(f.get("mark"))


def bps(f: dict[str, Any], ref: float | None = None) -> float | None:
    """The fill against `ref` (default ref_price) in bps, positive = worse for the short."""
    ref, px = ref or ref_price(f), _pos(f.get("px"))
    if not ref or not px:
        return None
    return (1 if f.get("leg") == "entry" else -1) * (ref - px) / ref * 1e4


def mult(f: dict[str, Any], costs: Costs = COSTS) -> float | None:
    kind = SIM_KIND.get(f"{f.get('leg')}/{f.get('type')}")
    return costs.charge(kind) if kind else None


def model_bps(f: dict[str, Any], vol24_usd: float | None, costs: Costs = COSTS) -> float | None:
    m = mult(f, costs)
    if m is None or (m and not vol24_usd):
        return None
    return m * costs.slip(vol24_usd) * 1e4 if m and vol24_usd else 0.0   # (m: vol24_usd is known, above)


def lag_ms(f: dict[str, Any]) -> int | None:
    return int(f["ref_ms"]) - int(f["ms"]) if f.get("ref_ms") and f.get("ms") else None


def _json(x: Any) -> dict[str, Any]:
    try:
        v = json.loads(x) if isinstance(x, str) else x
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _trades(j, all_modes: bool) -> list[dict[str, Any]]:
    m, args = j._in_mode(all_modes)
    rows = j._rows(f"SELECT id, symbol, setup, status, entry_features, fill_refs FROM trades WHERE {m} AND {NOT_DRILL}"
                   " ORDER BY id", args)   # a plumbing drill's fills are no evidence (#46)
    for r in rows:   # the doc's `orders` are the trade's order ids (order_ids.py, #76): a doc of only those has none
        refs = {k: v for k, v in _json(r.pop("fill_refs")).items() if k != "orders"}
        has = any(refs.get(k) for k in ("fills", "missed", "vol24_usd"))
        r["entry_features"], r["refs"] = _json(r["entry_features"]), refs if has else {}
    return rows


def _entry_type(t: dict[str, Any]) -> str:
    return "limit" if t["entry_features"].get("entry_type") == "limit" else "market"


def fills(j, all_modes: bool = False, costs: Costs = COSTS, trades: list[dict] | None = None) -> list[dict[str, Any]]:
    """One row per recorded fill, oldest trade first (also for shortbot.lab)."""
    out = []
    for t in trades if trades is not None else _trades(j, all_modes):
        vol = _pos(t["refs"].get("vol24_usd")) or _pos(t["entry_features"].get("quote_volume"))
        for f in t["refs"].get("fills") or []:
            lag = lag_ms(f)
            out.append({"trade_id": t["id"], "symbol": t["symbol"], "setup": t["setup"], "entry_type": _entry_type(t),
                        "key": f"{f.get('leg')}/{f.get('type')}", "leg": f.get("leg"), "order": f.get("order"),
                        "ms": f.get("ms"), "src": f.get("src"), "qty": _pos(f.get("qty")) or 1.0, "vol24_usd": vol,
                        "mult": mult(f, costs), "lag_ms": lag, "stale": lag is not None and abs(lag) > MAX_LAG_MS,
                        "bps": bps(f), "bps_mark": bps(f, _pos(f.get("mark"))), "model_bps": model_bps(f, vol, costs)})
    return out


def _wavg(rows: list[dict], field: str) -> float | None:
    xs = [(r[field], r["qty"]) for r in rows if r[field] is not None]
    return sum(v * w for v, w in xs) / sum(w for _, w in xs) if xs else None


def _mean(xs: list[float | None]) -> float | None:
    known = [x for x in xs if x is not None]
    return sum(known) / len(known) if known else None


def _per_trade(t: dict[str, Any], rows: list[dict]) -> dict[str, Any]:
    ok = [r for r in rows if not r["stale"] and r["bps"] is not None]
    leg = {k: [r for r in ok if r["leg"] == k] for k in ("entry", "exit")}
    got = {f"{k}_bps": _wavg(leg[k], "bps") for k in leg}
    got.update({f"model_{k}_bps": _wavg(leg[k], "model_bps") for k in leg})
    vals = [got["entry_bps"], got["exit_bps"], got["model_entry_bps"], got["model_exit_bps"]]
    known = [v for v in vals if v is not None]   # all four, else no excess
    missed = t["refs"].get("missed") or {}
    have = {r["order"] for r in rows}
    return {"trade_id": t["id"], "symbol": t["symbol"], "setup": t["setup"], "entry_type": _entry_type(t),
            "status": t["status"], **{k: _r(v) for k, v in got.items()},
            "excess_bps": _r(known[0] + known[1] - known[2] - known[3]) if len(known) == len(vals) else None,
            "fills": len(rows), "stale": sum(r["stale"] for r in rows), "lost": len(set(missed) - have)}


def _bucket(rows: list[dict]) -> dict[str, Any]:
    ok = [r for r in rows if not r["stale"] and r["bps"] is not None]
    cm = rs.clustered_mean([r["bps"] for r in ok], rs.days_of(ok, "ms"))
    model = _mean([r["model_bps"] for r in ok])
    return {"n": cm["n"], "days": cm["days"], "mean_bps": _r(cm["mean"]),
            "median_bps": _r(statistics.median([r["bps"] for r in ok])) if ok else None,
            "ci95": [_r(x) for x in cm["ci95"]] if cm["ci95"] else None, "n_eff": _r(cm["n_eff"], 1),
            "model_bps": _r(model), "excess_bps": _r(cm["mean"] - model) if ok and model is not None else None}


def _group(rows: list[dict], key: str) -> dict[str, dict[str, Any]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r[key] or "untagged", []).append(r)
    fields = ("entry_bps", "exit_bps", "model_entry_bps", "model_exit_bps", "excess_bps")
    return {k: {"n_trades": len(v), **{f: _r(_mean([r[f] for r in v])) for f in fields}} for k, v in out.items()}


def report(j, all_modes: bool = False, recent: int = RECENT) -> dict[str, Any]:
    """journal stats' fill_quality: coverage, by fill type, by setup, by entry type, the newest trades."""
    trades = _trades(j, all_modes)
    rows = fills(j, trades=trades)
    by_trade: dict[int, list[dict]] = {}
    for r in rows:
        by_trade.setdefault(r["trade_id"], []).append(r)
    with_refs = [t for t in trades if t["refs"]]
    per = [_per_trade(t, by_trade.get(t["id"], [])) for t in with_refs]
    keys = sorted({r["key"] for r in rows})
    ok = [r for r in rows if not r["stale"] and r["bps"] is not None]
    return {"how_to_read": HOW, "coverage": _coverage(trades, rows),
            "model": {"slip_k_bps": COSTS.slip_k_bps, "clip_bps": [COSTS.slip_min_bps, COSTS.slip_max_bps],
                      "stop_mult": COSTS.stop_slip_mult, "max_lag_ms": MAX_LAG_MS},
            "by_fill": {k: _bucket([r for r in rows if r["key"] == k]) for k in keys},
            "by_setup": _group(per, "setup"), "by_entry_type": _group(per, "entry_type"),
            "trades": per[-recent:][::-1], "note": rs.inference_note(len(set(rs.days_of(ok, "ms"))))}


def _coverage(trades: list[dict], rows: list[dict]) -> dict[str, Any]:
    missed = {(t["id"], o): why for t in trades for o, why in (t["refs"].get("missed") or {}).items()}
    have = {(r["trade_id"], r["order"]) for r in rows}
    exits = {r["trade_id"] for r in rows if r["leg"] == "exit"}
    lost = {k: why for k, why in missed.items() if k not in have}
    return {"closed_trades": sum(t["status"] == "closed" for t in trades),
            "trades_with_refs": sum(bool(t["refs"]) for t in trades), "fills": len(rows),
            "stale": sum(r["stale"] for r in rows), "missed": len(missed), "recovered": len(missed) - len(lost),
            "lost": len(lost), "lost_by_reason": dict(Counter(lost.values())),
            "closed_without_exit_ref": sum(t["status"] == "closed" and t["id"] not in exits for t in trades)}


def calibrate(j, all_modes: bool = False, min_trades: int = MIN_TRADES, min_days: int = rs.MIN_DAYS,
              costs: Costs = COSTS) -> dict[str, Any]:
    """Refit sim's slippage constant k from logged market and stop fills (module doc). ok=False below the sample:
    the numbers are then descriptive only and calibrated_costs() keeps the current model."""
    pts = [(r, r["mult"] / math.sqrt(r["vol24_usd"] / 1e6)) for r in fills(j, all_modes, costs)
           if not r["stale"] and r["bps"] is not None and r["mult"] and r["vol24_usd"]]
    trades, days = len({r["trade_id"] for r, _ in pts}), len(set(rs.days_of([r for r, _ in pts], "ms")))
    out: dict[str, Any] = {"n_fills": len(pts), "n_trades": trades, "days": days, "current_k_bps": costs.slip_k_bps,
                           "venue": "all modes" if all_modes else j.mode, "k_bps": None, "k_bps_median": None,
                           "n_eff": None, "ok": False}
    if pts:
        ratios = [r["bps"] / x for r, x in pts]
        out.update(k_bps=_r(sum(r["bps"] * x for r, x in pts) / sum(x * x for _, x in pts)),
                   k_bps_median=_r(statistics.median(ratios)),
                   n_eff=_r(rs.clustered_mean(ratios, rs.days_of([r for r, _ in pts], "ms"))["n_eff"], 1))
    out["ok"] = bool(trades >= min_trades and days >= min_days and (out["k_bps"] or 0) > 0)
    out["note"] = (f"calibrated on {trades} trades over {days} days ({out['venue']} fills: that venue's costs)"
                   if out["ok"] else f"descriptive only: {trades} trade(s) < {min_trades} or {days} day(s) < "
                   f"{min_days}; the backtest keeps k = {costs.slip_k_bps} bp")
    return out


def calibrated_costs(j, base: Costs = COSTS, **kw: Any) -> tuple[Costs, dict[str, Any]]:
    """The backtest's Costs with k refit from logged fills when calibrate() is ok, else `base` unchanged."""
    info = calibrate(j, costs=base, **kw)
    return (replace(base, slip_k_bps=info["k_bps"]) if info["ok"] else base), info

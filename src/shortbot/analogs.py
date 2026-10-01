"""Analog episodes (registry fine cut, tasks #75/#76, docs/REGISTRY.md): 'which past setups looked most like this
one, and what happened next?' - k nearest neighbours on z-scored, as-of features of past trades of the SAME
strategy family.

The panel must EARN its place (learning #14): `validate` trains on the in-sample half and predicts the out-of-sample
half. It is shown live only for families where the kNN prediction beats the family's plain mean OOS (rank
correlation > 0 with t >= 2 AND a positive top-minus-bottom quintile spread). Otherwise an analog panel is just a
persuasive way to dress a coin flip."""

from __future__ import annotations

import math
import warnings
from typing import Any

import numpy as np

NUM = ("atr_pct", "oi_vol", "beta", "corr", "ret_1h", "ret_4h", "ret_24h", "btc_ret_4h", "btc_ret_24h",
       "sector_ret_4h", "resid_4h", "oi_chg_4h", "oi_chg_24h", "funding_pct", "premium_bp", "ls_top", "ls_global",
       "taker_ratio", "ti_4h", "er_24h", "dist_high24_pct", "dist_round_pct")
LOGS = ("vol24_m", "oi_m", "age_d")
FLAGS = ("weekend", "has_spot", "false_break_24h")
K = 25


def vector(f: dict[str, Any]) -> list[float]:
    """Feature dict (backtest `features.py` names) -> numeric vector; missing values become NaN."""
    v = [float(f[k]) if isinstance(f.get(k), (int, float)) and not isinstance(f.get(k), bool) else math.nan
         for k in NUM]
    v += [math.log1p(max(float(f[k]), 0.0)) if isinstance(f.get(k), (int, float)) else math.nan for k in LOGS]
    v += [1.0 if f.get(k) else 0.0 for k in FLAGS]
    h = f.get("hour")
    v += [math.sin(2 * math.pi * h / 24), math.cos(2 * math.pi * h / 24)] if isinstance(h, (int, float)) \
        else [math.nan, math.nan]
    return v


class Analogs:
    """Fit on past trades [{f, r, symbol, entry_ts}]; query with a feature dict."""

    def __init__(self, trades: list[dict[str, Any]], k: int = K):
        self.trades = trades
        x = np.array([vector(t["f"]) for t in trades], dtype=float)
        with warnings.catch_warnings():            # all-missing columns (e.g. no L/S data) are fine: they drop out
            warnings.simplefilter("ignore", RuntimeWarning)
            self.mu = np.nan_to_num(np.nanmean(x, axis=0))
            sd = np.nan_to_num(np.nanstd(x, axis=0))
        self.sd = np.where(sd > 1e-12, sd, 1.0)
        self.z = np.nan_to_num((x - self.mu) / self.sd)     # missing -> the mean (0 after z-scoring)
        self.r = np.array([t["r"] for t in trades], dtype=float)
        self.k = min(k, len(trades))

    def query(self, f: dict[str, Any], exclude_symbol: str | None = None) -> dict[str, Any]:
        q = np.nan_to_num((np.array(vector(f), dtype=float) - self.mu) / self.sd)
        d = np.sqrt(((self.z - q) ** 2).sum(axis=1))
        if exclude_symbol:
            d = np.where(np.array([t["symbol"] == exclude_symbol for t in self.trades]), np.inf, d)
        idx = np.argsort(d)[: self.k]
        idx = idx[np.isfinite(d[idx])]
        if not len(idx):
            return {"pred_r": None, "win_pct": None, "k": 0, "mean_dist": None, "neighbours": []}
        w = 1.0 / (1.0 + d[idx])
        pred = float((w * self.r[idx]).sum() / w.sum())
        return {"pred_r": round(pred, 3), "win_pct": round(float((self.r[idx] > 0).mean() * 100), 1),
                "k": int(len(idx)), "mean_dist": round(float(d[idx].mean()), 2),
                "neighbours": [{"symbol": self.trades[i]["symbol"], "ts": self.trades[i].get("entry_ts"),
                                "r": round(float(self.r[i]), 2)} for i in idx[:8]]}


def _rank(a: np.ndarray) -> np.ndarray:
    return np.argsort(np.argsort(a)).astype(float)


def validate(trades: list[dict[str, Any]], mid_ms: int, k: int = K) -> dict[str, Any]:
    """Train on trades before `mid_ms`, predict the rest. Does the analog prediction rank OOS outcomes?"""
    tr = [t for t in trades if (t.get("exit_ts") or t["entry_ts"]) < mid_ms]   # purge: outcome known before mid
    te = [t for t in trades if t["entry_ts"] >= mid_ms]
    if len(tr) < 100 or len(te) < 50:
        return {"ok": False, "why": f"too few trades (train {len(tr)}, test {len(te)})"}
    model = Analogs(tr, k)
    pred = np.array([model.query(t["f"])["pred_r"] for t in te])
    real = np.array([t["r"] for t in te], dtype=float)
    rho = float(np.corrcoef(_rank(pred), _rank(real))[0, 1])
    t_rho = rho * math.sqrt((len(te) - 2) / max(1e-12, 1 - rho ** 2))
    q = np.quantile(pred, [0.2, 0.8])
    top, bot = real[pred >= q[1]], real[pred <= q[0]]
    spread = float(top.mean() - bot.mean()) if len(top) and len(bot) else 0.0
    return {"ok": True, "train": len(tr), "test": len(te), "spearman": round(rho, 3), "t": round(t_rho, 2),
            "top_quintile_r": round(float(top.mean()), 3), "bottom_quintile_r": round(float(bot.mean()), 3),
            "spread_r": round(spread, 3), "family_mean_r": round(float(real.mean()), 3),
            "useful": bool(rho > 0 and t_rho >= 2 and spread > 0)}


def validate_run(bt_dir: str, min_trades: int = 150) -> dict[str, Any]:
    """Validate the analog panel on a backtest output dir (results.json + trades.json): one row per strategy's
    selected variant. Run weekly by refresh.py; a family's panel goes live only while `useful` holds."""
    import json
    from pathlib import Path
    res = json.loads(Path(bt_dir, "results.json").read_text())
    chosen = {v["strategy"]: v["trial"] for v in (res.get("selected") or {}).values()
              if isinstance(v, dict) and v.get("trial")}
    by: dict[str, list[dict[str, Any]]] = {}
    for t in json.loads(Path(bt_dir, "trades.json").read_text()):
        if t.get("r") is not None and t.get("f") and chosen.get(t["strategy"]) == t.get("trial"):
            by.setdefault(t["strategy"], []).append(t)
    return {s: validate(ts, res["mid"]) for s, ts in by.items() if len(ts) >= min_trades}

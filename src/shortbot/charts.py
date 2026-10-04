"""Chart renderer for visual review (the analyst can Read the PNG). Everything drawn comes from the same DataHub
the signals use, so the picture and the numbers agree. The chart is for review; decisions come from signals
(Lo-Mamaysky-Wang: encode patterns, don't eyeball them).

Panels: candles + volume, VWAP ±2σ, liquidity levels (prior high, round number), open position entry/stop,
pending limit entries, failed-sweep marker, recorded liquidations, OI, CVD."""

from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import mplfinance as mpf
import pandas as pd

from . import tape_math as tm
from .market import DataHub
from .signals import evaluate
from .tape import classify

TF_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
OI_PERIOD = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h"}


def _frame(bars: list[list[float]]) -> pd.DataFrame:
    df = pd.DataFrame(bars, columns=["ts", "Open", "High", "Low", "Close", "Volume", "TakerBuy"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def _vwap_bands(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    cv = df["Volume"].cumsum().replace(0, math.nan)
    vwap = (tp * df["Volume"]).cumsum() / cv
    var = ((tp - vwap) ** 2 * df["Volume"]).cumsum() / cv
    sd = var.clip(lower=0) ** 0.5
    return vwap, vwap + 2 * sd, vwap - 2 * sd


async def render(ex, symbol: str, tf: str = "15m", bars: int = 150, out_dir: str | Path = "data/charts",
                 trader=None, entries: list[dict] | None = None) -> dict[str, Any]:
    hub = DataHub(ex)
    ctx = hub.context(symbol)
    flow = await ctx.flow_candles(tf, bars)
    if len(flow) < 20:
        return {"ok": False, "error": f"not enough {tf} data for {ctx.symbol}"}
    ev = await evaluate(ctx)
    tape = classify(ev.features)
    df = _frame(flow)
    vwap, upper, lower = _vwap_bands(df)
    cvd = pd.Series(tm.cvd(flow), index=df.index)
    oi_rows = await ctx.oi_history(OI_PERIOD.get(tf, "1h"), min(bars, 500))
    oi = pd.Series({pd.to_datetime(o["ts"], unit="ms", utc=True): o["oi_value"] / 1e6 for o in oi_rows})
    oi = oi.reindex(df.index, method="ffill") if len(oi) else pd.Series(math.nan, index=df.index)

    ap = [mpf.make_addplot(vwap, color="#1f77b4", width=1.0),
          mpf.make_addplot(upper, color="#1f77b4", linestyle="--", width=0.7),
          mpf.make_addplot(lower, color="#1f77b4", linestyle="--", width=0.7),
          mpf.make_addplot(oi, panel=2, color="#9467bd", ylabel="OI $M"),
          mpf.make_addplot(cvd, panel=3, color="#2ca02c", ylabel="CVD")]

    # failed-sweep marker on the wick bar
    s = tm.sweep_state(flow, 32)
    if s.get("state") == "failed_sweep":
        mark = pd.Series(math.nan, index=df.index)
        i = max(range(len(flow) - 3, len(flow)), key=lambda k: flow[k][2])
        mark.iloc[i] = flow[i][2] * 1.003
        ap.append(mpf.make_addplot(mark, type="scatter", marker="v", markersize=140, color="red"))

    # recorded liquidations inside the window
    since = int(flow[0][0])
    liqs = await ctx.liquidations(since)
    if liqs:
        longs, shorts = pd.Series(math.nan, index=df.index), pd.Series(math.nan, index=df.index)
        step = TF_MS.get(tf, 900_000)
        for x in liqs:
            k = min(max(int((x["ts_ms"] - since) // step), 0), len(df) - 1)
            (longs if x["side"] == "long_liquidated" else shorts).iloc[k] = x["price"]
        if longs.notna().any():
            ap.append(mpf.make_addplot(longs, type="scatter", marker="o", markersize=30, color="#d62728"))
        if shorts.notna().any():
            ap.append(mpf.make_addplot(shorts, type="scatter", marker="o", markersize=30, color="#2ca02c"))

    levels, colors, styles = [], [], []

    def level(price: float | None, color: str, style: str = "-.") -> None:
        if price:
            levels.append(float(price))
            colors.append(color)
            styles.append(style)

    level(s.get("level"), "#ff7f0e")          # prior 8h high (liquidity)
    level(s.get("round_level"), "#7f7f7f")    # psychological round number
    if trader is not None and ex.trade:
        for p in await trader.positions():
            if p["symbol"] == ctx.symbol:
                level(p["entry"], "#000000", "-")
                level(p["exchange_stop"], "#d62728", "-")
    for e in entries or []:
        if e["symbol"] == ctx.symbol:
            level(e["limit_price"], "#17becf", "--")
            level(e["stop_price"], "#d62728", "--")

    out = Path(out_dir)
    await asyncio.to_thread(out.mkdir, parents=True, exist_ok=True)
    path = out / f"{ctx.base}_{tf}_{int(time.time())}.png"
    title = f"{ctx.symbol} {tf} | tape: {tape['state']} ({tape['stance']}) | score {ev.score}"
    kw: dict[str, Any] = {"type": "candle", "style": "yahoo", "volume": True, "addplot": ap,
                          "panel_ratios": (4, 1, 1.2, 1.2), "figsize": (13, 9), "title": title,
                          "warn_too_much_data": 10_000,
                          "savefig": {"fname": str(path), "dpi": 100, "bbox_inches": "tight"}}
    if levels:
        kw["hlines"] = {"hlines": levels, "colors": colors, "linestyle": styles, "linewidths": 1}
    mpf.plot(df[["Open", "High", "Low", "Close", "Volume"]], **kw)
    return {"ok": True, "path": str(path.resolve()), "tape": tape, "score": ev.score, "vetoes": ev.vetoes,
            "signals": ev.signals, "legend": {
                "orange -.": "prior 8h high (liquidity)", "grey -.": "round number",
                "black/red solid": "open position entry / stop", "cyan/red dashed": "pending limit entry / stop",
                "red ▼": "failed sweep wick", "red/green dots": "long / short liquidations",
                "blue": "VWAP ±2σ", "panels": "volume, OI ($M), CVD"}}

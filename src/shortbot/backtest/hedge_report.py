"""Run the hedge overlay (hedge.py, docs/HEDGE.md) over a backtest trade list, a desk-like subset and the desk's
live trades, and print markdown tables.

    .venv/bin/python -m shortbot.backtest.hedge_report --trades DIR/trades.json --results DIR/results.json [--out OUT]

Writes hedge.md, hedge.json and provenance.json (default <data dir>/research/<UTC date>-<sha7>/hedge/; --no-save
prints only); `upstream` is the backtest's own provenance. Read-only: hist.db and shortbot.db are opened with
mode=ro; never touches the exchange or the journal."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import provenance as P
from .hedge import VARIANTS, Ctx, Series, beta, clip_ratio, leg, load_series, shrink_ratio, summarize, tide_up
from .hedge_diag import splits, trade_diag, verdicts
from .sim import H

STRATS = ("pump_fade", "pump_fade+discount", "post_pump_decay", "bounce_fade", "level_fade", "funding_hi",
          "listing_fade")
LIVE_VARIANTS = ("btc_1", "btc_ols", "btc_shrink", "btc_mv", "btc_cond", "eth_1", "alt_1", "alt_shrink")
RETIRED = {"level_fade", "post_pump_decay", "laggard", "delisting"}
WEIGHT = {"bounce_fade": 0.5}                     # probation: half size in the desk book
DESK = ("pump_fade", "pump_fade+discount", "bounce_fade", "funding_hi", "listing_fade")
COLS = ("n", "coins", "avg_r", "win_pct", "sd_r", "sr", "max_dd_r", "p5", "p1", "corr_btc", "is", "oos", "sr_is",
        "sr_oos", "t_day", "var_ratio", "hedge_r", "cost_r", "ratio")


def load_trades(path: Path, results: dict) -> dict[str, list[dict]]:
    want = {s: results["selected"][s]["trial"] for s in STRATS if s in results["selected"]}
    by_trial = {v: k for k, v in want.items()}
    out: dict[str, list[dict]] = {s: [] for s in want}
    for t in json.loads(path.read_text()):
        s = by_trial.get(t["trial"])
        if s:
            out[s].append(t)
    return out


def desk_ok(t: dict) -> bool:
    """Tide veto (strategies_v2.tide_ok thresholds) + liquidity tier B (risk.LIQ_TIERS: $5M vol, $10M OI)."""
    f = t.get("f") or {}
    b24, b4, s4 = f.get("btc_ret_24h"), f.get("btc_ret_4h"), f.get("sector_ret_4h")
    if (b24 is not None and b24 >= 2.0) or (b4 is not None and b4 >= 1.0):
        return False
    if f.get("sector") != "other" and s4 is not None and s4 >= 1.0:
        return False
    return (f.get("vol24_m") or 0) >= 5 and (f.get("oi_m") or 0) >= 10


# ------------------------------------------------------------ live trades
def _ms(iso: str | None) -> int | None:
    return int(datetime.fromisoformat(iso).timestamp() * 1000) if iso else None


def live_trades(ctx: Ctx, db_path: Path, variants=LIVE_VARIANTS) -> list[dict[str, Any]]:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = db.execute("SELECT id, symbol, setup, opened_at, closed_at, entry_price, exit_price, amount, "
                      "COALESCE(initial_stop, stop_price), pnl, status FROM trades ORDER BY opened_at").fetchall()
    db.close()
    last = max(ctx.btc.bars) + H
    out = []
    for tid, sym, setup, op, cl, ep, xp, amt, stop, pnl, status in rows:
        t_in, t_out = _ms(op), _ms(cl)
        rec: dict[str, Any] = {"id": tid, "symbol": sym, "setup": setup, "status": status, "pnl": pnl,
                               "notional": ep * amt, "risk_usd": (stop - ep) * amt if stop else None}
        if t_in is None or t_in >= last or t_in < min(ctx.btc.bars):
            rec["note"] = "outside hist.db window"
            out.append(rec)
            continue
        if t_out is None or t_out >= last:
            rec["note"] = "open / past hist.db end: marked at the last hist.db close"
            t_out = last - 1
        rec["r_short"] = pnl / rec["risk_usd"] if pnl is not None and rec["risk_usd"] else None
        coin = ctx.s.get(sym)
        t_sig = t_in // H * H - H                                   # last CLOSED hour before the entry
        b24 = _chg(ctx.btc, t_sig, 24)
        b4 = _chg(ctx.btc, t_sig, 4)
        rec.update(btc_ret_pct=_move(ctx.btc, t_in, t_out) * 100, btc24=b24, btc4=b4)
        for v in variants:
            inst, rule = VARIANTS[v]
            refs = ([ctx.eth] if inst == "ETH" else ctx.alts({"symbol": sym}, (t_in // H * H, "", 0, t_out // H * H))
                    if inst == "ALT" else [ctx.btc])
            if rule == "one":
                q = 1.0
            elif rule == "mv":
                q = clip_ratio(beta(coin, refs, t_sig, h=4, log=False) if coin else None)
            else:
                b = beta(coin, refs, t_sig) if coin else None
                q = clip_ratio(b) if rule == "ols" else shrink_ratio(b)
                if rule == "cond" and not tide_up({"btc_ret_24h": b24, "btc_ret_4h": b4}):
                    q = 0.0
            legs = [leg(r, r.interp(t_in), r.interp(t_out), t_in, t_out, t_sig, ctx.costs) for r in refs]
            usd = q * rec["notional"] * sum(lg["pnl"] for lg in legs) / len(legs)
            rec[v] = {"ratio": round(q, 2), "usd": round(usd, 2),
                      "r": round(usd / rec["risk_usd"], 3) if rec["risk_usd"] else None}
        out.append(rec)
    return out


def _chg(s: Series, ts: int, back: int) -> float | None:
    a, b = s.close(ts - back * H), s.close(ts)
    return (b / a - 1) * 100 if a and b else None


def _move(s: Series, t_in: int, t_out: int) -> float:
    return s.interp(t_out) / s.interp(t_in) - 1


# ------------------------------------------------------------ markdown
def _fmt(x) -> str:
    return "" if x is None else f"{x:+.3f}" if isinstance(x, float) and abs(x) < 10 else str(x)


def table(res: dict[str, dict], cols=COLS) -> str:
    ver = verdicts({v: d["all"] for v, d in res.items()})
    lines = ["| variant | " + " | ".join(cols) + " | verdict |", "|---" * (len(cols) + 2) + "|"]
    for v, d in res.items():
        lines.append(f"| {v} | " + " | ".join(_fmt(d["all"].get(c)) for c in cols) + f" | {ver.get(v, '')} |")
    return "\n".join(lines)


def section(ctx: Ctx, title: str, trades: list[dict], mid: int, weights: list[float] | None = None,
            diag: bool = True) -> tuple[str, dict]:
    """Every variant over `trades` (optionally weighted per trade) + regime / tail splits + rho^2 / idio stops."""
    res = {}
    for v in VARIANTS:
        rows = []
        for k, t in enumerate(trades):
            r = ctx.overlay(t, v)
            if r:
                w = weights[k] if weights else 1.0
                r.update(r=r["r"] * w, r0=r["r0"] * w, hedge_r=r["hedge_r"] * w, cost_r=r["cost_r"] * w)
                rows.append(r)
        res[v] = {"all": summarize(rows, mid), "rows": rows}
    sp = splits(res)
    js = {"stats": {v: d["all"] for v, d in res.items()}, "verdicts": verdicts({v: d["all"] for v, d in res.items()}),
          "splits": sp}
    md = [f"### {title}", "", table(res), "", "BTC over the trade (avg R, n): " + json.dumps(sp["regime"]),
          "", "squeeze tail, short lost >= 1R (avg R, n): " + json.dumps(sp["tail"]), ""]
    if diag:
        js["diag"] = trade_diag(ctx, trades)
        md += ["realized rho^2 (mean, n) and idiosyncratic stop share: " + json.dumps(js["diag"]), ""]
    return "\n".join(md), js


def live_md(live: list[dict]) -> str:
    vs = [k for k in LIVE_VARIANTS if any(k in r for r in live)]
    lines = ["| id | coin | setup | short PnL $ | short R | BTC % | " + " | ".join(f"{v} $ (R)" for v in vs) + " | note |",
             "|---" * (7 + len(vs)) + "|"]
    tot = {v: 0.0 for v in vs}
    tot_r = {v: 0.0 for v in vs}
    for r in live:
        cells = []
        for v in vs:
            d = r.get(v)
            if d:
                tot[v] += d["usd"]
                tot_r[v] += d["r"] or 0
                cells.append(f"{d['usd']:+.2f} ({_fmt(d['r'])}, x{d['ratio']})")
            else:
                cells.append("")
        lines.append(f"| {r['id']} | {r['symbol'].split('/')[0]} | {r['setup']} | {_fmt(r['pnl'])} | "
                     f"{_fmt(r.get('r_short'))} | {_fmt(r.get('btc_ret_pct'))} | " + " | ".join(cells)
                     + f" | {r.get('note', '')} |")
    lines.append("| total | | | " + _fmt(round(sum(r["pnl"] or 0 for r in live), 2)) + " | "
                 + _fmt(round(sum(r.get("r_short") or 0 for r in live), 3)) + " | | "
                 + " | ".join(f"{tot[v]:+.2f} ({tot_r[v]:+.2f})" for v in vs) + " | |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", type=Path, required=True)
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--hist", type=Path, help="default: <data dir>/hist.db")
    ap.add_argument("--live", type=Path, help="default: the journal (SHORTBOT_DB)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--no-save", action="store_true", help="print the markdown only")
    given = list(sys.argv[1:] if argv is None else argv)
    a = ap.parse_args(given)
    a.hist, a.live = a.hist or P.data_dir() / "hist.db", a.live or P.desk_db()
    inputs = {"hist_db": P.fingerprint(a.hist), "journal_trades": P.fingerprint(a.live, ["trades"], file_hash=False),
              "results": P.file_digest(a.results), "trades": P.file_digest(a.trades)}
    results = json.loads(a.results.read_text())
    mid = int(results["mid"])
    trades = load_trades(a.trades, results)
    ctx = Ctx(load_series(a.hist))
    md, js = [f"mid (IS/OOS split) = {mid}", ""], {"mid": mid, "strategies": {}, "desk": {}}
    for s, ts in trades.items():
        text, js["strategies"][s] = section(ctx, f"{s} ({results['selected'][s]['trial']})", ts, mid)
        md.append(text)
    md.append("## Desk-like subset (tide veto + liquidity tier B + registry gates; tape states not computable)")
    book: dict[tuple, tuple[dict, float]] = {}
    for s in DESK:
        if s not in trades or s in RETIRED:
            continue
        ts = [t for t in trades[s] if desk_ok(t)]
        text, js["desk"][s] = section(ctx, f"desk: {s}", ts, mid, diag=False)
        md.append(text)
        for t in ts:
            book.setdefault((t["symbol"], t["entry_ts"]), (t, WEIGHT.get(s, 1.0)))
    bt = sorted(book.values(), key=lambda x: x[0]["exit_ts"])
    text, js["desk"]["book"] = section(ctx, "desk book (union, deduplicated, bounce_fade at 0.5)",
                                       [t for t, _ in bt], mid, [w for _, w in bt])
    md.append(text)
    live = live_trades(ctx, a.live)
    js["live"] = live
    md += ["## Live trades (data/shortbot.db, read-only)", "", live_md(live), ""]
    text = "\n".join(md)
    prov = out = None
    if not a.no_save:
        given = P.ensure_arg(P.ensure_arg(given, "--hist", a.hist), "--live", a.live)
        prov = P.stamp(P.config("shortbot.backtest.hedge_report", given), inputs,
                       window=(results.get("provenance") or {}).get("window"), upstream=results.get("provenance"))
        text += "\n\n" + P.footer(prov) + "\n"
        out = P.save(a.out or P.run_dir("hedge", prov["git"]), prov, {
            "hedge.md": text, "hedge.json": json.dumps({**js, "provenance": prov}, indent=1, default=str)})
        text += f"\nsaved to {out}"
    from .register_rows import hedge_rows
    from .. import trial_register as TR
    print(text + f"\ntrial register: {TR.record('hedge', hedge_rows(js), prov, out)} rows")


if __name__ == "__main__":
    main()

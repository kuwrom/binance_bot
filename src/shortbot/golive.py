"""The go-live gate (#46 §4, docs/GO_LIVE.md): what the demo desk must have shown before the owner switches it to live.
Read-only: report(), `shortbot golive` and the brief's line only read the journal (no reader that may record, such as
setup_ladder.status or equity_marks, is called). Switching to live stays the owner's (.env, deploy/DEPLOY.md §6).

Sample: the closed demo trades (Journal.closed_results: never a plumbing drill's, #46) opened inside the window, which
starts at max(EPOCH, kv uptime_since): only time with a recorded guardian uptime counts. In live mode the gate still
reads the demo evidence (a demo view of the same journal); golive_view adds the live probation line.

Each criterion is {ok, value, threshold, source, summary, detail, gated}; the verdict is READY only when every gated one
passes. Every threshold names where it comes from (repo rule: parameters come from research and backtests):
- registry: >= 1 setup traded in the window is live-eligible: its strategy node (read.SETUP_STRATEGY) passed its frozen
  forward test (registry_forward.py: 30 trades over >= 20 days, one-sided day-clustered t, p <= 0.025 / M) and is not
  retired or on probation. The only promotion rule (docs/REGISTRY.md).
- sample: n_eff >= 30 independent trades (two-way day x coin clustering on the entry day, Kish: cluster_se) and >= 20
  entry days.
- expectancy: lower 95% bound of mean R > 0 (two-way clustered se, t(min(days, coins) - 1)) on the worse of demo R and
  mainnet-repriced R: each trade's R moved by its fills' excess over the backtest's slippage model (fill_report, #35),
  i.e. the same fills at the mainnet price plus the model's slippage.
- stress: mean R > 0 at 1.5x fees and 2x slippage (backtest/run.STRESS: the backtest's SURVIVES bar), each trade at the
  worse of its demo and mainnet R.
- drawdown: each setup's max drawdown of cumulative R <= its setup-ladder halve level (setup_ladder.thresholds: the 80%
  quantile of the 100-trade max DD of a +0.10R setup; a bootstrap from 50 trades). Light mode skips the bootstrap and
  uses its floor (CLAMP[0] x prior), never a looser bound.
- equity: drawdown of equity < the governor's halve tier (dd_halve_pct), from the finest series the journal keeps (each
  UTC day's first equity and its intraday low and high), net of transfers and demo resets (golive_equity.py).
- uptime: uptime recorded (kv uptime_since); no untagged guardian gap, nor one open now, past the session watchdog's
  stale limit. Drill-tagged gaps are planned outages.
- incidents: zero unprotected-position alerts in the window; near-misses are listed, not gated.
- slippage: fill_report.calibrate on the window's trades ok (>= 30 trades with market or stop fills over >= 20 days,
  k > 0).
- plumbing: golive_view.plumbing (drills, probes, backup drill, gateway soak; each within 30 days).
- parameters (reported, not gated): the window's trades sized under the .env baseline the live guardian uses."""

from __future__ import annotations

import copy
import json
import statistics
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from . import cluster_se as C
from . import fill_report as FR
from . import golive_equity as GE
from . import golive_view, uptime
from . import killswitch as ks
from . import registry as R
from . import registry_forward as RF
from . import research_stats as rs
from . import setup_ladder as L
from .backtest.drawdown import max_drawdown
from .backtest.sim import COSTS, Costs
from .config import RiskLimits

# guardian and journal on EC2 since 2026-10-03 (deploy/DEPLOY.md; plan-46 §4): the Mac-era demo ran another stack and
# has no uptime record
EPOCH = "2026-10-03T00:00:00+00:00"
MIN_N_EFF = RF.MIN_TARGET_N      # 30: the registry's sample floor; n < 30 is an anecdote (docs/RESEARCH.md)
MIN_DAYS = rs.MIN_DAYS           # 20: fewer day clusters and an interval is descriptive only (Cameron & Miller 2015 §VI.A)
MAX_GAP_S = ks.STALE_HEARTBEAT_S  # 600 s: past it the session watchdog trips HALTED (killswitch.py)
LIVE_PROBATION_N = RF.MIN_TARGET_N   # the first 30 live trades: the forward test's sample, now on live fills
# protection was missing or wrong on the exchange (guardian_positions.py, stops.py, trading_*.py: the kinds
# drill_restart.BAD_ALERTS also refuses)
INCIDENTS = ("NAKED_POSITION", "stop_missing", "stop_restore_failed", "untracked_position", "unexpected_long",
             "position_flip")
# protection held, or was restored, after something went wrong: listed for review, not gated ("*" = a prefix)
NEAR_MISSES = ("stop_restored", "stop_failed", "forced_close", "bridge_left", "bridge_swept", "stop_left",
               "widen_undone", "close_incomplete", "close_unknown", "fill_unresolved", "orphan_stop_*")
OWNER_STEPS = (
    "This report only reads. Only the owner edits .env or switches to live: deploy/DEPLOY.md §6.",
    "1. Create the live key (DEPLOY §2); add BINANCE_LIVE_API_KEY and BINANCE_LIVE_API_SECRET to .env.",
    "2. Set SHORTBOT_MODE=live and LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK.",
    "3. Keep the limits small: MAX_POSITION_NOTIONAL, MAX_TOTAL_NOTIONAL, RISK_PER_TRADE_PCT.",
    "4. `docker compose up -d` (restarts the guardian), then restart Claude Code.",
    f"Start at minimum size: the first {LIVE_PROBATION_N} live trades are probation (`shortbot golive` in live mode "
    "compares their fills with the model and with demo).")


def crit(ok: bool, value: Any, threshold: str, source: str, summary: str, detail: Any = None,
         gated: bool = True) -> dict[str, Any]:
    return {"ok": bool(ok), "value": value, "threshold": threshold, "source": source, "summary": summary,
            "detail": detail, "gated": gated}


def _r(x: float | None, nd: int = 3) -> float | None:
    return None if x is None else round(x, nd)


def demo_view(j: Any) -> Any:
    """The journal as its demo reader (closed_results, mode_key, gaps and alerts by mode): a shallow copy, no writes."""
    if j.mode == "demo":
        return j
    view = copy.copy(j)
    view.mode = "demo"
    return view


def window_start(j: Any) -> str:
    return max(EPOCH, uptime.uptime_since(j) or EPOCH)


# ------------------------------------------------------------------ the sample
def _stress() -> Costs:
    from .backtest.run import STRESS  # (lazy: the brief imports this module)
    return STRESS


def _extra_slip_bps(per: dict[str, Any], vol: float, stress: Costs) -> float:
    """What 2x slippage adds to a trade, in bps of its notional: the model's charge for its recorded fills again at the
    stress k; a trade without fill refs pays a market entry and a stop exit (the sim's dearest pair)."""
    s, b = stress.slip(vol), COSTS.slip(vol)
    me, mx = per.get("model_entry_bps"), per.get("model_exit_bps")
    if me is not None and mx is not None:
        return float((me + mx) * (s / b - 1))
    return (1 + COSTS.stop_slip_mult) * (s - b) * 1e4


def sample(j: Any, start: str, limits: RiskLimits | None = None) -> list[dict[str, Any]]:
    """The closed trades opened since `start`, oldest close first, each with its demo R (r), its R at mainnet prices
    (r_main: + its fills' excess over the model, in R) and its stress R (the worse of the two, less 0.5x its fees and
    1x more model slippage). A trade without fill refs keeps r_main = r (priced False)."""
    rows = [r for r in j.closed_results() if r.get("r_multiple") is not None and r.get("risk_usdt")
            and (r.get("opened_at") or "") >= start]
    if not rows:
        return []
    stress, floor = _stress(), (limits or RiskLimits()).min_quote_volume_24h
    refs = {t["id"]: t for t in FR._trades(j, False)}
    by: dict[int, list[dict[str, Any]]] = {}
    for f in FR.fills(j, trades=[refs[r["id"]] for r in rows if r["id"] in refs]):
        by.setdefault(f["trade_id"], []).append(f)
    fees = {int(a): float(b or 0.0) for a, b in j.db.execute("SELECT id, fees FROM trades WHERE status='closed'")}
    out = []
    for r in rows:
        t = refs.get(r["id"]) or {"refs": {}, "entry_features": {}}
        per = FR._per_trade(t, by.get(r["id"], [])) if t["refs"] else {}
        risk, notional = float(r["risk_usdt"]), float(r["entry_price"] or 0) * float(r["amount"] or 0)
        excess = per.get("excess_bps")
        r_main = r["r_multiple"] + (excess * notional / 1e4 / risk if excess is not None else 0.0)
        vol = FR._pos(t["refs"].get("vol24_usd")) or FR._pos(t["entry_features"].get("quote_volume")) or floor
        cost = abs(fees.get(r["id"], 0.0)) * (stress.taker_bps / COSTS.taker_bps - 1) \
            + notional * _extra_slip_bps(per, vol, stress) / 1e4
        out.append({"id": r["id"], "symbol": r["symbol"], "setup": r["setup"], "day": r["opened_at"][:10],
                    "closed_at": r["closed_at"], "r": r["r_multiple"], "r_main": r_main,
                    "r_stress": min(r["r_multiple"], r_main) - cost / risk, "priced": excess is not None,
                    "excess_bps": excess, "limits": r.get("limits"), "experiment": r.get("experiment")})
    return out


def infer(trades: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """Mean of `key` with its lower 95% bound: two-way (day x coin) clustered se, t(min(G, H) - 1); one coin: by day.
    n_eff = s^2 / V (Kish): the iid sample with the same precision."""
    vals = [float(t[key]) for t in trades]
    inf = C.mean_inference(vals, [t["day"] for t in trades], [t["symbol"] for t in trades])
    out: dict[str, Any] = {"n": inf["n"], "mean": _r(inf["mean"]), "days": inf["days"], "coins": inf["coins"],
                           "lower": None, "se": None, "df": None, "n_eff": None, "clusters": None}
    if inf["se_2way"] is not None:
        se, df, how, n_eff = inf["se_2way"], inf["df_2way"], "day x coin", inf["n_eff"]
    elif inf["se_day"] is not None:
        se, df, how = inf["se_day"], inf["df_day"], "day (one coin)"
        n_eff = min(float(len(vals)), statistics.variance(vals) / se ** 2) if se > 0 else float(len(vals))
    else:
        return out
    out.update(lower=_r(inf["mean"] - rs.t975(df) * se), se=_r(se, 4), df=df, n_eff=_r(n_eff, 1), clusters=how)
    return out


# ------------------------------------------------------------------ the criteria
def registry_crit(j: Any, trades: list[dict[str, Any]]) -> dict[str, Any]:
    fam, nodes = RF.family(j.db), RF.setup_strategy()
    detail: dict[str, dict[str, Any]] = {}
    for s in sorted({t["setup"] for t in trades if t["setup"]}):
        pid = nodes.get(s)
        cur = R.latest(j.db, pid) if pid else None
        if cur is None or pid is None:
            detail[s] = {"eligible": False, "node": pid, "why": f"{pid} is not registered" if pid else
                         "no strategy node (read.SETUP_STRATEGY): not promotable; trade it as exp_ in demo"}
            continue
        fwd = RF.forward(j.db, pid, cur["version"], fam)
        ok = fwd.get("status") == "decided" and fwd.get("decision") == "pass" and \
            cur["status"] not in ("retired", "probation")
        detail[s] = {"eligible": ok, "node": pid, "version": cur["version"], "status": cur["status"],
                     "forward": fwd.get("status"), "decision": fwd.get("decision"), "test": fwd.get("test"),
                     "interim": fwd.get("interim"), "why": RF.verdict(cur["status"], fwd, None)}
    elig = [s for s, d in detail.items() if d["eligible"]]
    return crit(bool(elig), elig, f"a setup's forward test decided pass (>= {RF.MIN_TARGET_N} trades over >= "
                f"{rs.MIN_DAYS} days, one-sided day-clustered t, p <= {RF.ALPHA} / M), not retired or on probation",
                "registry_forward.py (#26, docs/REGISTRY.md 'Forward test'): the only rule that promotes to live",
                f"live-eligible: {', '.join(elig) or 'none'} of {len(detail)} traded setup(s)", detail)


def sample_crit(trades: list[dict[str, Any]]) -> dict[str, Any]:
    inf = infer(trades, "r")
    ok = (inf["n_eff"] or 0) >= MIN_N_EFF and inf["days"] >= MIN_DAYS
    return crit(ok, {k: inf[k] for k in ("n", "n_eff", "days", "coins")},
                f"n_eff >= {MIN_N_EFF} and >= {MIN_DAYS} entry days",
                "n_eff: cluster_se.mean_inference (Cameron, Gelbach & Miller 2011; Kish); 30 = registry_forward."
                "MIN_TARGET_N; 20 days = research_stats.MIN_DAYS (Cameron & Miller 2015 §VI.A)",
                f"n_eff {inf['n_eff']} of {inf['n']} trades, {inf['days']} entry days, {inf['coins']} coins")


def expectancy_crit(trades: list[dict[str, Any]]) -> dict[str, Any]:
    demo, main = infer(trades, "r"), infer(trades, "r_main")
    lows = [x for x in (demo["lower"], main["lower"]) if x is not None]
    lower = min(lows) if len(lows) == 2 else None
    basis = "mainnet" if lower is not None and main["lower"] < demo["lower"] else "demo"
    unpriced = sum(not t["priced"] for t in trades)
    return crit(lower is not None and lower > 0, {"lower": lower, "basis": basis, "demo": demo, "mainnet": main,
                                                 "unpriced": unpriced},
                "lower 95% bound of mean R > 0 on the worse of demo R and mainnet-repriced R",
                "two-way clustered se (cluster_se, CGM 2011), t(min(days, coins) - 1); repricing: fill_report "
                "excess_bps (#35, demo fills vs the mainnet reference and the backtest's slippage model)",
                f"mean R demo {demo['mean']} (lower {demo['lower']}), mainnet {main['mean']} (lower {main['lower']})"
                + (f"; {unpriced} trade(s) without fill refs kept at demo R" if unpriced else ""))


def stress_crit(trades: list[dict[str, Any]]) -> dict[str, Any]:
    mean = statistics.fmean(t["r_stress"] for t in trades) if trades else None
    return crit(mean is not None and mean > 0, _r(mean), "mean R > 0 at 1.5x fees and 2x slippage",
                "backtest/run.py STRESS (1.5x fees, 2x slippage); docs/BACKTEST.md: SURVIVES needs stress R > 0",
                f"stress mean R {_r(mean)}" if mean is not None else "no closed trades in the window")


def _halve(setup: str, xs: list[float], light: bool) -> tuple[float, str]:
    if light and len(xs) >= L.MIN_N:   # the bootstrap's floor: never looser than the full report
        return round(L.CLAMP[0] * L.prior(setup)[0], 2), "prior x 0.75 (light: no bootstrap)"
    th = L.thresholds(setup, xs)
    return float(th["halve"]), str(th["source"])


def drawdown_crit(trades: list[dict[str, Any]], light: bool = False) -> dict[str, Any]:
    by: dict[str, list[float]] = {}
    for t in trades:   # closed_results order: oldest close first
        by.setdefault(t["setup"] or "untagged", []).append(float(t["r"]))
    detail: dict[str, dict[str, Any]] = {}
    for s, xs in sorted(by.items()):
        dd, (halve, src) = round(max_drawdown(xs), 2), _halve(s, xs, light)
        detail[s] = {"n": len(xs), "max_dd_r": dd, "halve_r": halve, "basis": src, "ok": dd <= halve}
    worst = max(detail.items(), key=lambda kv: kv[1]["max_dd_r"] / kv[1]["halve_r"], default=None)
    return crit(bool(detail) and all(d["ok"] for d in detail.values()), worst[1]["max_dd_r"] if worst else None,
                "each setup's max drawdown of cumulative R <= its setup-ladder halve level",
                "setup_ladder.thresholds (docs/PROPOSALS.md P2; backtest/drawdown.py: 80% quantile of the 100-trade max "
                "DD of a +0.10R setup, Bailey & López de Prado 2015)",
                f"{worst[0]} max DD {worst[1]['max_dd_r']}R vs halve {worst[1]['halve_r']}R" if worst
                else "no closed trades in the window", detail)


def equity_crit(j: Any, start: str, limits: RiskLimits) -> dict[str, Any]:
    d = GE.drawdown(j, start)
    worst, jumps = d["max_dd_pct"], d["jumps"]
    where = f"{d['at']} {d['kind']}" if d["at"] else "no drawdown"
    each = ", ".join(f"{x['jump']:+.2f} USDT {x['from']}→{x['to']}" for x in jumps[:3]) + ", …" * (len(jumps) > 3)
    net = (f"; {len(jumps)} jump{'s' * (len(jumps) != 1)} netted ({each}): a transfer, a demo reset or P&L the "
           "journal doesn't hold") if jumps else ""
    return crit(worst is not None and worst < limits.dd_halve_pct, worst,
                f"< {limits.dd_halve_pct}% drawdown of equity (day marks and intraday range), net of transfers and "
                "resets", "the governor's halve tier dd_halve_pct (config.RiskLimits, .env baseline; "
                "docs/RISK_GOVERNANCE.md 'Drawdown from peak'); series and netting: golive_equity.py",
                f"max {worst:.2f}% ({where}) over {d['marks']} day marks and {d['ranges']} intraday ranges (marks "
                f"alone {d['marks_only_pct']:.2f}%){net}" if d["marks"] else "no daily equity marks in the window", d)


def uptime_crit(j: Any, start: str, now: datetime) -> dict[str, Any]:
    s = uptime.summary(j, start, now.timestamp())
    bad = [g for g in s["gaps"] if not g["tag"] and g["secs"] > MAX_GAP_S]
    open_now = s["open_gap_s"] if (s["open_gap_s"] or 0) > MAX_GAP_S else None
    ok = s["covered"] and s["heartbeat_age_s"] is not None and not bad and open_now is None
    drill = sum(1 for g in s["gaps"] if g["tag"])
    return crit(ok, max(float(s["untagged_max_s"]), open_now or 0.0),
                f"uptime recorded; no untagged gap (or one open now) > {MAX_GAP_S:g} s",
                "uptime.py guardian_gaps (#46); limit = killswitch.STALE_HEARTBEAT_S, the watchdog's HALTED trip",
                ("no uptime record (kv uptime_since)" if not s["covered"] else
                 f"{len(s['gaps'])} gap(s), {s['downtime_s']:g} s down ({drill} drill-tagged), longest untagged "
                 f"{s['untagged_max_s']:g} s" + (f", down now for {open_now:g} s" if open_now else "")),
                {**{k: v for k, v in s.items() if k != "gaps"}, "untagged_over": bad})


def _near(kind: str) -> bool:
    return any(kind.startswith(k[:-1]) if k.endswith("*") else kind == k for k in NEAR_MISSES)


def incidents_crit(j: Any, start: str) -> dict[str, Any]:
    rows = j._rows("SELECT id, ts, kind, symbol FROM alerts WHERE ts >= ? AND COALESCE(mode, ?) = ? ORDER BY id",
                   (start, j.mode, j.mode))
    inc = [r for r in rows if r["kind"] in INCIDENTS]
    near = dict(Counter(r["kind"] for r in rows if _near(r["kind"])))
    return crit(not inc, len(inc), f"0 alerts of {', '.join(INCIDENTS)}",
                "guardian_positions.py, stops.py, trading_*.py (alerts_view.URGENT_KINDS): a position without its "
                "stop or a position the desk didn't open",
                f"{len(inc)} incident(s)" + (f"; near-misses: {', '.join(f'{k} {n}' for k, n in near.items())}"
                                              if near else ""),
                {"by_kind": dict(Counter(r["kind"] for r in inc)), "incidents": inc[-10:], "near_misses": near})


def slippage_crit(j: Any, start: str) -> dict[str, Any]:
    c = FR.calibrate(j, since=start)
    return crit(c["ok"], c["k_bps"], f">= {FR.MIN_TRADES} trades opened in the window with market or stop fills over "
                f">= {rs.MIN_DAYS} days, k > 0", "fill_report.calibrate (#35): the backtest's slip k refit from the "
                "desk's own fills, those of the window's trades (since=)",
                f"{c['note']}; k {c['k_bps']} bp vs model {c['current_k_bps']} bp", c)


def plumbing_crit(j: Any, now: datetime) -> dict[str, Any]:
    p = golive_view.plumbing(j, now)
    return crit(p["ok"], f"{p['current']}/{p['total']} current",
                f"each drill's latest result a pass, each probe matching its fake (or explained), the backup drill "
                f"passed, the gateway soak clean: each within {golive_view.VALID_DAYS:g} days",
                "plan-46 §3b; deploy/DEPLOY.md §7a (drills), probes.py (#55), docs/GATEWAY.md D6 (soak)",
                "all current" if p["ok"] else "; ".join(p["blockers"][:4])
                + (f" (+{len(p['blockers']) - 4} more)" if len(p["blockers"]) > 4 else ""), p)


def parameters(trades: list[dict[str, Any]], limits: RiskLimits) -> dict[str, Any]:
    base, known, match = dict(vars(limits)), 0, 0
    differs: dict[str, set[Any]] = {}
    for t in trades:
        try:
            lim = json.loads(t["limits"]) if isinstance(t["limits"], str) else t["limits"]
        except ValueError:
            lim = None
        if not isinstance(lim, dict):
            continue
        known += 1
        diff = {k: lim[k] for k in base if k in lim and lim[k] != base[k]}
        match += not diff
        for k, v in diff.items():
            differs.setdefault(k, set()).add(v)
    unknown = len(trades) - known
    value = f"{match}/{known} trades sized under the .env baseline" + (
        f" ({unknown} without recorded limits)" if unknown else "")
    return crit(match == known, value, "reported, not gated",
                "params.py: AI overrides apply in demo only; the live guardian uses the .env baseline (CLAUDE.md)",
                value, {"differs": {k: sorted(v, key=str) for k, v in differs.items()}, "baseline": base,
                        "experiments": dict(Counter(t["experiment"] or "baseline" for t in trades))}, gated=False)


def report(j: Any, limits: RiskLimits | None = None, light: bool = False, now: datetime | None = None
           ) -> dict[str, Any]:
    """The gate on the demo evidence. light: no bootstrap (the brief's line)."""
    now, limits = now or datetime.now(UTC), limits or RiskLimits()
    d = demo_view(j)
    start = window_start(d)
    trades = sample(d, start, limits)
    crits = {"registry": registry_crit(d, trades), "sample": sample_crit(trades),
             "expectancy": expectancy_crit(trades), "stress": stress_crit(trades),
             "drawdown": drawdown_crit(trades, light), "equity": equity_crit(d, start, limits),
             "uptime": uptime_crit(d, start, now), "incidents": incidents_crit(d, start),
             "slippage": slippage_crit(d, start),
             "plumbing": plumbing_crit(d, now), "parameters": parameters(trades, limits)}
    blockers = [k for k, c in crits.items() if c["gated"] and not c["ok"]]
    out: dict[str, Any] = {
        "verdict": "NOT READY" if blockers else "READY", "blockers": blockers, "mode": j.mode, "light": light,
        "window": {"from": start, "epoch": EPOCH, "uptime_since": uptime.uptime_since(d),
                   "to": now.isoformat(timespec="seconds")},
        "criteria": crits, "owner_steps": list(OWNER_STEPS),
        "how_to_read": "READY only when every gated criterion passes, on demo evidence since the window start. Each "
                       "criterion names its threshold and its source; detail holds the evidence. Read-only."}
    if j.mode == "live":
        out["live_probation"] = golive_view.probation(j, d, start, limits)
    return out

"""Margin and auto-deleverage facts of open isolated shorts (#64, docs/PROPOSALS.md P1b/P1c), read from Binance's own
fields. Pure: the guardian acts on them (guardian_margin.py), and the stop paths share stop_cap (trading_manage.py).

Sources (the doc pages, as Binance's generated SDK carries them: binance-connector-python,
clients/derivatives_trading_usds_futures):
- GET /fapi/v1/adlQuantile (USER_DATA, weight 5): values update every 30 s; 0-4 = the ADL queue position, low to high;
  One-way Mode answers LONG, SHORT and BOTH (our position: BOTH); crossed Hedge Mode answers HEDGE instead of BOTH.
  Without `symbol` a list ([] when flat: demo, read-only call 2026-10-02), with one a single object (JKorf/Binance.Net
  #1107). A populated answer is unverified on demo: read tolerantly, and say what could not be read.
- GET /fapi/v3/positionRisk (PositionInformationV3ResponseInner): isolatedMargin = isolatedWallet + unrealized PnL
  (ccxt's coin-M sample adds up), maintMargin, marginAsset; no marginType (ccxt 4.5.84 parse_position_risk: 'cross'
  iff isolatedMargin == "0"). ccxt's own maintenanceMargin leaves out the bracket's cum, so maintMargin is read raw.
- Binance ADL FAQ: effective leverage = |notional| / (wallet balance + unrealized PnL), ranking = PnL% x effective
  leverage: a winning short climbs the queue. ADL closes it at the bankrupt counterparty's price and cancels the
  symbol's open orders (isolated). For a short that is lost upside, not a loss: only the top bucket alerts.
- GET /fapi/v1/multiAssetsMargin (USER_DATA, weight 30): {"multiAssetsMargin": bool}. Multi-Assets mode and isolated
  margin exclude each other (-4167 / -4168, ccxt binance.py ~2190)."""

from __future__ import annotations

from typing import Any

ADL_TOP = 4                # the top bucket of 0-4: first in line
ADL_PERSIST_PASSES = 15    # watch passes (60 s) in a row at the top: ~15 min, ~30 fresh values
MARGIN_MIN_MULT = 2.0      # isolated margin under 2x maintenance margin is thin (liquidation at 1x)
MARGIN_ASSETS = ("USDT", "USDC")


def num(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _quantile(v: Any) -> int | None:
    n = num(v)
    return int(n) if n is not None and n == int(n) and 0 <= n <= 4 and not isinstance(v, bool) else None


def parse_adl(resp: Any) -> tuple[dict[str, int], list[str]]:
    """({raw symbol: quantile of the one-way position}, what could not be read). BOTH is ours; without it SHORT,
    noted."""
    rows = [resp] if isinstance(resp, dict) else resp if isinstance(resp, list) else None
    if rows is None:
        return {}, [f"answer is {type(resp).__name__}, not a list or object: {str(resp)[:200]}"]
    got: dict[str, int] = {}
    bad: list[str] = []
    for r in rows:
        q = r.get("adlQuantile") if isinstance(r, dict) else None
        sym = r.get("symbol") if isinstance(r, dict) else None
        if not sym or not isinstance(q, dict):
            bad.append(f"row without symbol / adlQuantile: {str(r)[:200]}")
        elif "HEDGE" in q:
            bad.append(f"{sym}: HEDGE (crossed Hedge Mode): {q}")
        elif (quantile := _quantile(q.get("BOTH", q.get("SHORT")))) is None:
            bad.append(f"{sym}: no quantile 0-4 in {q}")
        else:
            got[sym] = quantile
            if "BOTH" not in q:
                bad.append(f"{sym}: no BOTH (One-way Mode answers it), SHORT used: {q}")
    return got, bad


def effective_leverage(info: dict) -> float | None:
    """|notional| / isolatedMargin (the isolated wallet + unrealized PnL: the FAQ's denominator). None when crossed or
    unreadable."""
    n, m = num(info.get("notional")), num(info.get("isolatedMargin"))
    return round(abs(n) / m, 2) if n and m and m > 0 else None


def stop_cap(liq: float | None, buf_pct: float) -> float | None:
    """The highest stop on a short that still fires before liquidation: liq minus buf_pct (#17). None without one."""
    return liq * (1 - buf_pct / 100) if liq and liq > 0 else None


def multi_assets(resp: Any) -> bool:
    """GET /fapi/v1/multiAssetsMargin -> on? Raises ValueError on an answer it can't read (the job fails loudly)."""
    v = resp.get("multiAssetsMargin") if isinstance(resp, dict) else None
    if isinstance(v, bool):
        return v
    if str(v).lower() in ("true", "false"):
        return str(v).lower() == "true"
    raise ValueError(f"unexpected multiAssetsMargin answer: {str(resp)[:200]}")


def position_findings(p: dict) -> list[tuple[str, str]]:
    """(alert kind, message) per margin breach of one ccxt position row (Trader.raw_positions). margin_check_error:
    a field it needs can't be read, so that check did not run."""
    info = p.get("info") or {}
    mode = p.get("marginMode")
    if mode == "cross":
        return [("margin_not_isolated", "CROSS margin: this desk assumes isolated (losses here can take the whole "
                 "account, and #17's liquidation math is off). Binance won't switch it while a position exists "
                 "(-4048): close it, or reduce and re-enter isolated")]
    out: list[tuple[str, str]] = []
    if mode != "isolated":
        out.append(("margin_check_error", f"margin mode unreadable ({mode!r}, isolatedMargin "
                    f"{info.get('isolatedMargin')!r}): isolated NOT verified"))
    asset = info.get("marginAsset")
    if asset not in MARGIN_ASSETS:
        out.append(("margin_asset", f"margin asset {asset}: the desk assumes USDT/USDC margin") if asset else
                   ("margin_check_error", "marginAsset missing: the margin asset is NOT verified"))
    m, mm = num(info.get("isolatedMargin")), num(info.get("maintMargin"))
    if m is None or mm is None:
        out.append(("margin_check_error", f"isolatedMargin {info.get('isolatedMargin')!r} / maintMargin "
                    f"{info.get('maintMargin')!r} unreadable: the margin buffer is NOT checked"))
    elif mode == "isolated" and mm > 0 and m < MARGIN_MIN_MULT * mm:
        out.append(("margin_thin", f"isolated margin {m:.2f} is {m / mm:.1f}x the maintenance margin {mm:.2f} (Binance "
                    f"liquidates at 1x; floor {MARGIN_MIN_MULT:g}x): add margin or reduce"))
    return out

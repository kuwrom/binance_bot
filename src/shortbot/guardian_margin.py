"""Guardian margin and ADL checks (#64, docs/PROPOSALS.md P1b/P1c; facts and sources in margin.py).

- Watch lane: one ADL read per pass for every short (GET /fapi/v1/adlQuantile without a symbol, weight 5). The top
  bucket alerts, ADL_PERSIST_PASSES in a row escalate once (urgent), and an answer without a held short's quantile is
  a watch failure (#57: alerted, counted in the watch streak), logged loudly with the raw answer. The first non-empty
  answer is kept (kv adl_sample) for the demo probe.
- Protective positions pass, after the stop checks and never instead of them: each short isolated, margined in
  USDT/USDC, isolated margin >= 2x maintenance, and its resting stop inside liquidation (Trader.cap_stop tightens it).
- Monitor lane: Multi-Assets mode (GET /fapi/v1/multiAssetsMargin, weight 30) is an urgent alert.

All of it alerts only, except cap_stop (an intent, #36: gateway.py): a stop past liquidation never fires, so a stop at
the cap is strictly better.
A thin margin blocks nothing: isolated margin bounds the loss to this position, a second entry on the symbol is
refused anyway ("already short"), and adds already check liquidation against the stop (risk.check_add)."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from . import margin
from .exchange import to_raw
from .gateway import handler
from .guardian_loop import send_safe
from .intents import Intent
from .symlock import SymbolBusy

if TYPE_CHECKING:
    from .exchange import Exchanges
    from .gateway import Gateway
    from .guardian_loop import Streak
    from .journal import Journal
    from .notify import Notifier

log = logging.getLogger("shortbot.guardian")
ADL_SAMPLE_KEY = "adl_sample"
ADL_LOG_EVERY_S = 900          # the quantile and effective leverage, logged on a change and at least this often
ADL_ALERT_COOLDOWN_S = 3600
MARGIN_ALERT_COOLDOWN_S = 3600
MULTI_ASSETS_COOLDOWN_S = 4 * 3600


class MarginChecks:
    """Guardian mixin; the Guardian attributes it uses are declared below."""
    ex: Exchanges
    j: Journal
    n: Notifier
    gw: Gateway
    adl_streak: Streak
    _adl_logged: dict[str, tuple[int, float]]

    # ------------------------------------------------------------ watch lane (ADL)
    async def adl_read(self) -> tuple[dict[str, int], list[str], str | None]:
        """(raw symbol -> quantile, what couldn't be read, the read's error)."""
        try:
            resp = await self.ex.require_trade().fapiPrivateGetAdlQuantile({})
        except Exception as e:  # noqa: BLE001 - each short's watch result carries it
            return {}, [], f"adl: adlQuantile read failed: {type(e).__name__}: {e}"
        key = self.j.mode_key(ADL_SAMPLE_KEY)   # per desk: the live shape, once (unverified on demo)
        if resp and self.j.get(key) is None:
            raw = json.dumps(resp, default=str)[:4000]
            self.j.mark(key, raw)
            log.info("adlQuantile first non-empty answer: %s", raw)
        got, bad = margin.parse_adl(resp)
        if bad:
            log.error("adlQuantile answer not as documented (verify on demo): %s; raw: %.1000s", bad, resp)
        return got, bad, None

    async def adl_check(self, p: dict, got: dict[str, int], bad: list[str], err: str | None) -> str | None:
        """Alert / escalate one short's quantile. Returns why it couldn't be checked (a watch error), else None."""
        sym, raw = p["symbol"], to_raw(p["symbol"])
        if err:
            return err
        q = got.get(raw)
        if q is None:
            log.error("adlQuantile answer has no quantile for %s, which we hold short: %s", raw, bad or "missing")
            return f"adl: no quantile for {raw} in the adlQuantile answer" + \
                (f" ({'; '.join(bad)[:300]})" if bad else "")
        info = p.get("info") or {}
        lev = margin.effective_leverage(info)
        self._adl_log(sym, q, lev, info.get("adl"))
        lev_s = f"{lev:.1f}x" if lev else "unknown"
        if q >= margin.ADL_TOP:
            await self.n.send("adl_risk", f"ADL quantile {q}/4: first in line to be auto-deleveraged if a long goes "
                              f"bankrupt beyond the insurance fund (effective leverage {lev_s}, uPnL "
                              f"{margin.num(p.get('unrealizedPnl')) or 0:+.2f} USDT). ADL closes it at the bankrupt long's price and cancels this "
                              "symbol's open orders. Playbook: consider a partial take-profit (lowers effective "
                              "leverage).", sym, cooldown_s=ADL_ALERT_COOLDOWN_S)
            if self.adl_streak.fail(sym):
                await send_safe(self.n, "adl_risk_persistent", f"top ADL quantile for {self.adl_streak.after} passes "
                                f"in a row since {self.adl_streak.started(sym)} (effective leverage {lev_s}): take "
                                "a partial profit or accept an exit Binance chooses.", sym, urgent=True, cooldown_s=0)
        else:
            r = self.adl_streak.ok(sym)
            if r:
                await send_safe(self.n, "adl_risk_cleared", f"ADL quantile {q} after {r[0]} passes at the top "
                                f"({r[1] / 60:.0f} min)", sym, cooldown_s=0)
        return None

    def _adl_log(self, sym: str, q: int, lev: float | None, v3_adl) -> None:
        last = self._adl_logged.get(sym)
        if last and last[0] == q and time.time() - last[1] < ADL_LOG_EVERY_S:
            return
        self._adl_logged[sym] = (q, time.time())
        log.info("adl %s quantile %s, effective leverage %s (notional / isolated margin); positionRisk adl %s", sym, q,
                 f"{lev:.1f}" if lev else None, v3_adl)

    # ------------------------------------------------------------ protective positions pass
    async def margin_pass(self, live: list[dict], held: list[dict]) -> None:
        """After the stop checks: every short's margin facts, and a resting stop past liquidation tightened inside it.
        One short's failure is alerted and never costs the others."""
        rows = {p["symbol"]: p for p in held}
        buf = self.ex.limits.liq_buffer_pct
        for p in [p for p in live if p.get("side") == "short"]:
            sym = p["symbol"]
            try:
                for kind, msg in margin.position_findings(p):
                    if kind == "margin_check_error":
                        log.error("margin check %s: %s; raw: %.500s", sym, msg, p.get("info"))
                    await send_safe(self.n, kind, msg, sym, urgent=True, cooldown_s=MARGIN_ALERT_COOLDOWN_S)
                h = rows.get(sym) or {}
                cap = margin.stop_cap(margin.num(h.get("liquidation")), buf)
                if cap is not None and h.get("exchange_stop") and h["exchange_stop"] > cap:
                    await self._cap_stop(sym, h)
            except SymbolBusy as e:   # a session acts on it: the next pass looks again
                log.info("margin pass %s skipped: %s", sym, e)
            except Exception as e:  # noqa: BLE001 - never the other shorts', never the stop checks'
                log.exception("margin pass %s failed", sym)
                await send_safe(self.n, "margin_check_error", f"margin checks failed: {type(e).__name__}: {e}", sym,
                                urgent=True, cooldown_s=MARGIN_ALERT_COOLDOWN_S)

    async def _cap_stop(self, sym: str, h: dict) -> None:
        r = await self.gw.submit(Intent("cap_stop", {"symbol": sym}, sym))
        if not r:
            return
        did = {"tightened": f"tightened to {r['to']:g}", "closed": f"price {r['mark']:g} already past {r['to']:g}: "
               "CLOSED", "failed": f"NOT tightened ({r.get('error')})"}[r["action"]]
        plan = (self.j.active_trade(sym) or {}).get("stop_price")
        fix = f"The plan's stop {plan:g} stays in the journal: fix it, add margin or reduce." if plan else \
            "No journal trade: no plan stop to fix."
        await send_safe(self.n, "plan_stop_unreachable", f"resting stop {r['from']:g} is past liquidation "
                        f"{r['liquidation']:g} minus {self.ex.limits.liq_buffer_pct:g}%: it would never fire "
                        f"(liquidation first, plus its fee). {did}. {fix}", sym, urgent=True,
                        cooldown_s=MARGIN_ALERT_COOLDOWN_S)

    # ------------------------------------------------------------ monitor lane
    async def check_margin_mode(self) -> None:
        """Multi-Assets mode on: every entry's isolated switch is refused (-4168), and positions margin in cross."""
        if not self.ex.trade:
            return
        if margin.multi_assets(await self.ex.trade.fapiPrivateGetMultiAssetsMargin({})):
            await self.n.send("multi_assets_mode", "Multi-Assets mode is ON: this desk assumes single-asset USDT "
                              "isolated margin. Isolated margin is refused under it (-4168), so every entry fails, and "
                              "non-USDT collateral backs positions. Switch it off in Binance.", urgent=True,
                              cooldown_s=MULTI_ASSETS_COOLDOWN_S)


@handler("cap_stop", late_ok=True, ctx="guardian")
async def cap_stop(g, symbol: str) -> dict | None:
    """CapStop (GATEWAY.md §4.2, the margin pass's intent): Trader.cap_stop, decided again under the symbol lock."""
    return await g.app.trader.cap_stop(symbol)

"""Guardian position jobs. Protective lane: fills (5 s), the stop/plan pass (60 s) and entry sends of unknown outcome
(60 s), which touch only the trade client and the journal. Watch lane: alert-only checks on open shorts (squeeze,
funding, news) that read public data and third-party feeds, so they never delay a stop check. Alert thresholds live
here."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from . import indicators as ind
from . import killswitch as ks
from . import entry_intents, news, reconcile
from .exchange import to_raw
from .guardian_loop import guarded, send_safe
from .killswitch import ACTIVE, HALTED, REDUCING
from .killswitch_ops import cancel_resting
from .manager import excursions, plan_actions
from .risk_budget import track_day_peak
from .stops import is_clock_drift
from .symlock import SymbolBusy

log = logging.getLogger("shortbot.guardian")
SQUEEZE_PRICE_1H_PCT = 5.0
SQUEEZE_OI_1H_PCT = 5.0
STOP_NEAR_PCT = 1.0
RESYNC_EVERY_S = 600
FALLBACK_STOP_PCT = 10.0
MAX_STOP_RESTORE_FAILS = 3
UNTRACKED_GRACE_S = 120     # untracked this long (and no limit of ours resting on it) = reconciliation trip
HALT_SWEEP_TIMEOUT_S = 60   # a hung cancel must not hold up the fill poll behind it


class PositionChecks:
    """Guardian mixin: needs app, ex, j, n, entries, stop_fails, pos_streak, _last_resync, _fund_ts, untracked_since,
    kill_seen."""

    # ------------------------------------------------------------ positions (protective, 60 s)
    async def check_positions(self) -> None:
        if not self.ex.trade:
            return
        tr = self.app.trader
        if time.time() - self._last_resync > RESYNC_EVERY_S:   # the Mac's clock drifts after sleep (-1021)
            await self.ex.resync_time()
            self._last_resync = time.time()
        live = await tr._raw_positions()
        for res in await reconcile.reconcile(tr, live):
            await self.n.send("trade_closed", f"{res['reason']}: net {res['net']:+.2f} USDT (fees {res['fees']:.2f}, "
                              f"funding {res['funding']:.2f})", res["symbol"], cooldown_s=0, urgent=res["liquidated"])
        acct = await tr.account(live)
        track_day_peak(self.j, acct["day_pnl"])   # arms the give-back stop (governor.py)
        lim = self.app.settings.limits
        if acct["equity"] and acct["day_pnl"] / acct["equity"] * 100 <= -lim.daily_loss_limit_pct:
            await self.n.send("daily_loss_limit", f"Day PnL {acct['day_pnl']} USDT. New shorts are blocked until "
                              "00:00 UTC. Review open positions.", urgent=True, cooldown_s=6 * 3600)
        await guarded("kill_switch", ks.trip_on_daily_loss(self.j, self.app.settings, acct["equity"],
                                                           acct["day_pnl"]), self.n)   # effective limit, sticky
        held = await tr.positions()
        for p in held:
            sym = p["symbol"]
            try:
                await self._one_position(p)
            except SymbolBusy as e:   # another process is acting on it: skip this pass, retry next pass
                await self._position_busy(sym, e)
            except Exception as e:  # noqa: BLE001 - isolate: other positions still get checked
                await self._position_failed(sym, e)
            else:
                r = self.pos_streak.ok(sym)
                if r:
                    await send_safe(self.n, "guardian_recovered:position", f"position check OK again after {r[0]} "
                                    f"failures ({r[1] / 60:.0f} min)", sym, cooldown_s=0)
        self.pos_streak.forget(set(self.pos_streak.fails) - {p["symbol"] for p in held})   # closed meanwhile
        self.untracked_since = {s: t for s, t in self.untracked_since.items() if s in {p["symbol"] for p in held}}

    async def _position_failed(self, sym: str, e: Exception) -> None:
        """One symbol's failure stays out of the pass result, but a streak of them escalates (ONDO, -1021)."""
        escalate = self.pos_streak.fail(sym)
        if is_clock_drift(e):
            try:
                await self.ex.resync_time()   # the lane-level resync never sees errors swallowed here
            except Exception:  # noqa: BLE001
                log.exception("time resync failed")
        log.exception("position %s failed", sym)
        await send_safe(self.n, "position_check_error", f"{type(e).__name__}: {e}", sym, cooldown_s=1800)
        if escalate:
            await send_safe(self.n, "guardian_failing:position", f"check failed {self.pos_streak.after}x in a row "
                            f"since {self.pos_streak.started(sym)} (last: {type(e).__name__}: {e}). Its stop and plan "
                            "are NOT being verified: check it on the exchange.", sym, urgent=True, cooldown_s=0)

    async def _position_busy(self, sym: str, e: SymbolBusy) -> None:
        """A session holds the symbol's lock (symlock.py; the guardian waits GUARDIAN_WAIT_S at most, so one busy
        symbol never stalls the others). Not a failure: only a symbol that stays busy pass after pass escalates."""
        log.info("position %s skipped this pass: %s", sym, e)
        if self.pos_streak.fail(sym):
            await send_safe(self.n, "guardian_failing:position", f"busy {self.pos_streak.after} passes in a row since "
                            f"{self.pos_streak.started(sym)} ({e}). Its stop and plan are NOT being verified: check "
                            "what holds it (a hung or suspended session?).", sym, urgent=True, cooldown_s=0)

    async def _one_position(self, p: dict) -> None:
        sym = p["symbol"]
        if p["side"] != "short":
            await self.n.send("unexpected_long", f"A LONG position exists ({p['contracts']}). This desk is "
                              "short-only; close it manually.", sym, urgent=True)
            await guarded("kill_switch", ks.trip(self.j, self.app.settings, REDUCING, "guardian", f"a {p['side']} "
                          f"position ({p['contracts']}) in {sym} on a short-only desk", {"symbol": sym},
                          cause="long_position"), self.n)
            return
        if p["UNPROTECTED"]:
            await self._restore_stop(sym)
            return
        if p["journal_status"] == "NOT IN JOURNAL":
            await self.n.send("untracked_position", "Position not opened via shortbot: stop is enforced but no "
                              "plan/thesis. Journal it or close it.", sym, cooldown_s=86400)
        await self._untracked_trip(p)
        if p["stop_distance_pct"] is not None and p["stop_distance_pct"] < STOP_NEAR_PCT:
            await self.n.send("stop_near", f"Mark {p['mark']} is {p['stop_distance_pct']}% from stop "
                              f"{p['exchange_stop']}. PnL {p['unrealized_pnl']} USDT.", sym, cooldown_s=1800)
        trade = self.j.active_trade(sym)
        if trade and trade["status"] == "open" and trade.get("entry_price"):
            await self._run_plan(trade, p)
        try:
            await self._track_funding(sym)
        except Exception as e:  # noqa: BLE001 - tracking must never fail the stop checks
            log.warning("funding tracking %s: %s", sym, e)

    async def _untracked_trip(self, p: dict) -> None:
        """A position the journal doesn't know is a reconciliation mismatch: trip REDUCING once it has stayed untracked
        for UNTRACKED_GRACE_S with nothing explaining it on the symbol: a limit of ours that is (or may be) on the book,
        whose fill the poll or the reconciler is about to journal, or an external order that filled (entry_intents)."""
        sym = p["symbol"]
        try:
            if p["journal_status"] != "NOT IN JOURNAL" or sym in entry_intents.explained_symbols(self.entries):
                self.untracked_since.pop(sym, None)
                return
            age = time.time() - self.untracked_since.setdefault(sym, time.time())
            if age >= UNTRACKED_GRACE_S:
                await ks.trip(self.j, self.app.settings, REDUCING, "guardian", f"reconciliation: {sym} position "
                              f"({p['contracts']}) not in the journal for {age / 60:.0f} min",
                              {"symbol": sym, "contracts": p["contracts"]}, cause="reconciliation")
        except Exception as e:  # noqa: BLE001 - a kill-switch hook never costs the stop/plan checks below
            log.exception("untracked trip %s failed", sym)
            await send_safe(self.n, "guardian_error:kill_switch", f"{type(e).__name__}: {e}", sym, cooldown_s=1800)

    async def _restore_stop(self, sym: str) -> None:
        tr = self.app.trader
        try:
            res = await tr.ensure_stop(sym, FALLBACK_STOP_PCT)
            self.stop_fails.pop(sym, None)
            await self.n.send("stop_missing", f"No exchange stop found. Action: {res}", sym, urgent=True, cooldown_s=300)
        except SymbolBusy:
            raise   # someone else is acting on it: not a failed restore (check_positions skips it this pass)
        except Exception as e:  # noqa: BLE001
            n = self.stop_fails[sym] = self.stop_fails.get(sym, 0) + 1
            if n >= MAX_STOP_RESTORE_FAILS:
                async with tr.symbol_lock(sym, "guardian: forced close (no stop)"):
                    pos = await tr.position(sym)   # still due? another process may have protected or closed it
                    covered = bool(pos) and bool(await tr.stop_orders(sym, close_position_only=True))
                    if pos and not covered:
                        await tr._market_close(sym, float(pos["contracts"]))
                self.stop_fails.pop(sym, None)
                await self.n.send("forced_close", f"Could not restore a stop {n}x ({e}); " + (
                    "a stop is in place now (set elsewhere): NOT closed." if covered else "position CLOSED."), sym,
                                  urgent=True, cooldown_s=0)
            else:
                await self.n.send("stop_restore_failed", f"attempt {n}/{MAX_STOP_RESTORE_FAILS}: {e}", sym,
                                  urgent=True, cooldown_s=0)

    async def _run_plan(self, trade: dict, p: dict) -> None:
        ex = excursions(trade, p["mark"])
        self.j.update_trade(trade["id"], mfe_pct=round(ex["mfe_pct"], 3), mae_pct=round(ex["mae_pct"], 3),
                            low_since_entry=ex["low_since_entry"])
        trade = {**trade, **ex}
        tr, sym = self.app.trader, trade["symbol"]
        acts = plan_actions(trade, p)
        if all(a.kind == "alert" for a in acts):
            return await self._apply_plan(trade, p, acts)
        async with tr.symbol_lock(sym, "guardian plan"):
            # decide again on what is true NOW: a session may have closed, added or moved the stop since the snapshot
            pos = await tr.position(sym)
            fresh = self.j.get_trade(trade["id"])
            if not pos or not fresh or fresh["status"] != "open":
                return
            stop = min((s["trigger"] for s in await tr.stop_orders(sym)), default=None)
            p = {"mark": float(pos["markPrice"]), "contracts": float(pos["contracts"]), "exchange_stop": stop}
            trade = {**fresh, **excursions(fresh, p["mark"])}
            await self._apply_plan(trade, p, plan_actions(trade, p))

    async def _apply_plan(self, trade: dict, p: dict, acts: list) -> None:
        tr, sym = self.app.trader, trade["symbol"]
        hits, cur = list(trade.get("targets_hit") or []), p["contracts"]
        for a in acts:
            if a.kind == "alert":
                await self.n.send(f"plan:{a.reason.split(':')[0][:30]}", a.reason, sym, cooldown_s=a.cooldown_s,
                                  urgent=a.urgent)
            elif a.kind == "move_stop":
                r = await tr.move_stop(sym, a.price, f"plan/{a.reason}")
                if r.get("noop"):          # rounds to the stop already in place
                    continue
                await self.n.send("plan_stop_moved" if r.get("ok") else "plan_stop_move_failed",
                                  f"{a.reason} -> {a.price:.6g}: {r}", sym, cooldown_s=0)
            elif a.kind == "target_done":
                hits.append(a.target_price)                 # one running list: two actions in one cycle both stick
                self.j.update_trade(trade["id"], targets_hit=list(hits))
                await self.n.send("plan_target_done", a.reason, sym, cooldown_s=0)
            elif a.kind == "close":
                r = await tr.close(sym, f"plan/{a.reason}", fraction=a.fraction or 1.0, expect_contracts=cur)
                cur = r.get("remaining", cur)   # T1 and T2 in one pass: the second sees what the first left
                if a.target_price is not None and r.get("ok"):
                    hits.append(a.target_price)
                    self.j.update_trade(trade["id"], targets_hit=list(hits))
                await self.n.send("plan_close", f"{a.reason}: {r}", sym, cooldown_s=0)
                if (a.fraction or 1.0) >= 1.0:
                    return

    async def _track_funding(self, sym: str) -> None:
        """Funding paid so far on the open trade (every 15 min), so the plan's funding budget can act on it."""
        trade = self.j.active_trade(sym)
        if not trade or not trade.get("opened_at") or not self.ex.trade:
            return
        if time.time() - self._fund_ts.get(sym, 0) < 900:
            return
        self._fund_ts[sym] = time.time()
        since = int(datetime.fromisoformat(trade["opened_at"]).timestamp() * 1000)
        rows = await self.ex.trade.fapiPrivateGetIncome({"symbol": to_raw(sym), "incomeType": "FUNDING_FEE",
                                                         "startTime": since, "limit": 1000})
        self.j.update_trade(trade["id"], funding=round(sum(float(r.get("income") or 0) for r in rows), 6))

    # ------------------------------------------------------------ limit entries (protective, 5 s)
    async def check_entries(self) -> None:
        state = await self._kill_watch()
        if not self.ex.trade:
            return
        if state == HALTED:   # every pass: catches entries a session placed before it saw the trip
            await self._halt_sweep()
        if not self.entries.pending():
            return
        for ev in await self.entries.poll():
            if ev["event"] in ("cancelled", "expired"):
                await self.n.send(f"entry_{ev['event']}", f"entry #{ev['entry_id']}: {ev.get('why', '')}",
                                  cooldown_s=0)
            elif ev["event"] in ("filled", "tranche_joined", "sibling_joined") and state not in (None, ACTIVE):
                await send_safe(self.n, "fill_while_" + state.lower(), f"entry #{ev['entry_id']} filled while the "
                                f"kill switch is {state}: protected and journalled as trade #{ev.get('trade_id')}. "
                                "Manage it under its plan or close it; it is never auto-closed.", urgent=True,
                                cooldown_s=0)

    async def _kill_watch(self) -> str | None:
        """Each 5 s tick: lift a stale-heartbeat halt once protection works again (this guardian's own heartbeat),
        and announce every loosening. That is the tripwire: a clear that bypassed shortbot-admin still reaches the
        human. Returns the state, or None if it can't be read (the fill poll runs regardless)."""
        try:
            await ks.auto_clear_stale(self.j, self.app.settings)
            st, seen = ks.read(self.j), self.kill_seen
            self.kill_seen = st
            if seen is None and st["state"] != ACTIVE:
                await send_safe(self.n, "kill_switch_state", f"guardian started with the kill switch {st['state']} "
                                f"since {st.get('at')} ({st.get('by')}: {st.get('reason')})", cooldown_s=0)
            elif seen and st["level"] < seen["level"]:
                sanctioned = (st.get("cause") == "auto_clear" or bool(st.get("restored_at"))   # stale halt lifted
                              or str(st.get("by")).startswith("human:"))
                await send_safe(self.n, "kill_switch_loosened", f"{seen['state']} -> {st['state']} by {st.get('by')}: "
                                f"{st.get('reason')}" + ("" if sanctioned else ". NOT via shortbot-admin: check who "
                                                         "changed it"), urgent=not sanctioned, cooldown_s=0)
            return st["state"]
        except Exception as e:  # noqa: BLE001 - never fail the protective pass (it would stall the heartbeat)
            log.exception("kill-switch watch failed")
            await send_safe(self.n, "guardian_error:kill_switch", f"{type(e).__name__}: {e}", cooldown_s=1800)
            return None

    async def _halt_sweep(self) -> None:
        try:
            r = await asyncio.wait_for(cancel_resting(self.entries, "kill switch HALTED (guardian sweep)"),
                                       HALT_SWEEP_TIMEOUT_S)
            if r["cancelled"]:
                await send_safe(self.n, "kill_switch_sweep", f"HALTED: withdrew resting entries {r['cancelled']}"
                                + (f"; left for the fill poll {r['left_for_fill_poll']}" if r["left_for_fill_poll"]
                                   else ""), cooldown_s=0)
            for x in self.entries._rows("status='external'"):   # not the desk's (maybe the owner's): never cancelled
                await send_safe(self.n, f"external_while_halted:{x['order_id']}", f"external order {x['order_id']} on "
                                f"{x['symbol']} ({x['amount']} @ {x['limit_price']}) still resting while HALTED; "
                                "cancel it on Binance if it must not fill", x["symbol"], urgent=True, cooldown_s=3600)
        except Exception as e:  # noqa: BLE001 (incl. TimeoutError) - the fill poll below must still run
            log.exception("HALTED sweep failed")
            await send_safe(self.n, "guardian_error:kill_switch", f"HALTED sweep: {type(e).__name__}: {e}",
                            urgent=True, cooldown_s=1800)

    async def reconcile_entries(self) -> None:
        """Entry sends whose outcome the journal doesn't know, resolved by client id (entry_intents.py; it alerts)."""
        if self.ex.trade:
            await entry_intents.reconcile(self.entries)

    # ------------------------------------------------------------ watch (alert-only, 60 s, own lane)
    async def watch_positions(self) -> None:
        if not self.ex.trade:
            return
        for p in await self.app.trader._raw_positions():
            if p.get("side") != "short":
                continue
            for check in (self._squeeze_and_funding, self._news):   # each on its own: a dead feed never hides a squeeze
                try:
                    await check(p["symbol"])
                except Exception as e:  # noqa: BLE001
                    log.exception("watch %s failed", p["symbol"])
                    await self.n.send("position_watch_error", f"{type(e).__name__}: {e}", p["symbol"], cooldown_s=1800)

    async def _squeeze_and_funding(self, sym: str) -> None:
        pub = self.ex.public
        c5, oi, fr, intervals = await asyncio.gather(
            pub.fetch_ohlcv(sym, "5m", limit=13), self.ex.oi_history(sym, "5m", 13),
            pub.fetch_funding_rate(sym), self.ex.funding_intervals(), return_exceptions=True)
        if isinstance(c5, list) and len(c5) >= 13:
            px = ind.pct_change(c5[0][4], c5[-1][4])
            oi_chg = ind.pct_change(oi[0]["oi"], oi[-1]["oi"]) if isinstance(oi, list) and len(oi) > 1 else 0
            if px >= SQUEEZE_PRICE_1H_PCT and oi_chg >= SQUEEZE_OI_1H_PCT:
                await self.n.send("squeeze_risk", f"Price +{px:.1f}% and OI +{oi_chg:.1f}% in 1h. Re-evaluate now.",
                                  sym, urgent=True, cooldown_s=1800)
            elif px >= SQUEEZE_PRICE_1H_PCT * 1.5:
                await self.n.send("price_spike", f"Price +{px:.1f}% in 1h against the short.", sym, urgent=True,
                                  cooldown_s=1800)
        if isinstance(fr, dict) and (fr.get("fundingRate") or 0) < 0:
            await self.n.send("funding_negative", f"Funding {fr['fundingRate'] * 100:.4f}%: shorts now PAY and the "
                              "trade is crowded. Playbook: consider cutting.", sym, cooldown_s=4 * 3600)
        if isinstance(intervals, dict) and intervals.get(to_raw(sym), {}).get("interval_h") == 1:
            await self.n.send("funding_hourly", "Funding switched to 1h settlement (cap hit): extreme regime.", sym,
                              urgent=True, cooldown_s=4 * 3600)

    async def _news(self, sym: str) -> None:
        res = await news.news_for(sym.split("/")[0], max_items=5)
        for item in res["binance_announcements"] + res["headlines"]:
            url = item.get("url", "")
            if url and self.j.get(f"news_seen:{url}") is None:
                self.j.set(f"news_seen:{url}", str(time.time()))
                await self.n.send("news_on_position", f"{item['title']}\n{url}", sym, cooldown_s=0)

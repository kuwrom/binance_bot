"""Guardian position jobs. Protective lane: fills (5 s), the stop/plan pass (60 s) and entry sends of unknown outcome
(60 s), which touch only the trade client and the journal. The watch lane (alert-only checks on open shorts) lives in
guardian_watch.py. Alert thresholds live here.

A stop restore and a forced close are intents (#36 step 2, gateway.py), actor guardian:<job>: the pass decides, the
handlers below run it (with the guardian as their context). So are the fill poll's, the sweeps' and the reconciler's
sends (step 2b: entries_acts.py, through the desk's gateway). The reconcile job also ends the intents of a process that
died mid-run (intents.sweep)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from . import entry_gates, entry_intents, errors, intents, reconcile, resolver, stops
from . import killswitch as ks
from .exchange import to_raw
from .gateway import handler
from .guardian_loop import guarded, send_safe
from .intents import Intent
from .journal import Journal
from .killswitch import ACTIVE, HALTED, REDUCING
from .risk_budget import track_day_low, track_day_peak
from .symlock import SymbolBusy

if TYPE_CHECKING:
    from .app import App
    from .entries import EntryDesk
    from .exchange import Exchanges
    from .gateway import Gateway
    from .guardian_loop import Streak
    from .notify import Notifier
    from .user_stream import UserStream

log = logging.getLogger("shortbot.guardian")
STOP_NEAR_PCT = 1.0
RESYNC_EVERY_S = 600
FALLBACK_STOP_PCT = 10.0
MAX_STOP_RESTORE_FAILS = 3
UNTRACKED_GRACE_S = 120     # untracked this long (and no limit of ours resting on it) = reconciliation trip
ACCOUNT_TIMEOUT_S = 30      # the pass's account read (balance, positions, income): the fill poll waits behind it
PNL_UNVERIFIED_PASSES = 3   # passes in a row without a day P&L (about 3 min) before REDUCING
PNL_UNVERIFIED, PNL_KEY = "day_pnl_unverified", "day_pnl"   # the trip's cause; the streak's key
SEEN_KEY = "trading_state_seen"   # last state the guardian saw (via journal.mode_key): the tripwire survives restarts


class PositionChecks:
    """Guardian mixin; the Guardian attributes and the Guardian, MarginChecks and PlanChecks methods it uses are
    declared below."""
    app: App
    ex: Exchanges
    j: Journal
    n: Notifier
    entries: EntryDesk
    gw: Gateway
    user_stream: UserStream | None
    stop_fails: dict[str, int]
    pos_streak: Streak
    pnl_streak: Streak
    pnl_tripped: bool
    untracked_since: dict[str, float]
    kill_seen: dict | None
    _last_resync: float
    _fund_ts: dict[str, float]

    if TYPE_CHECKING:
        def _heartbeat(self, at: float) -> None: ...

        def _triggered(self, job: str) -> bool: ...

        async def margin_pass(self, live: list[dict], held: list[dict]) -> None: ...

        async def _settle_flat(self, live: list[dict]) -> None: ...

        async def _targets(self, coro: Awaitable[Any], sym: str | None = None, default: Any = None) -> Any: ...

        async def _run_plan(self, trade: dict, p: dict) -> None: ...

    # ------------------------------------------------------------ positions (protective, 60 s)
    async def check_positions(self) -> None:
        """The stop/plan pass, and the only stamp of the heartbeat (#61): after a pass in which every held position's
        check completed with its stop confirmed, or the account read flat. Without trade keys nothing is verified."""
        if not self.ex.trade:
            return
        tr = self.app.trader
        if time.time() - self._last_resync > RESYNC_EVERY_S:   # the Mac's clock drifts after sleep (-1021)
            await self.ex.resync_time()
            self._last_resync = time.time()
        t0, verified = time.time(), True   # protection is verified as of this read, not of the pass's end
        live = await tr.raw_positions()
        await self._targets(self._settle_flat(live))   # their fills first: a target exit reads as one (#65)
        try:   # journalling closed trades must never cost the stop checks below
            closed = await reconcile.reconcile(tr, live)
        except Exception as e:  # noqa: BLE001
            log.exception("reconcile failed")
            await send_safe(self.n, "reconcile_error", f"{type(e).__name__}: {e}", cooldown_s=1800)
            closed = []
        await reconcile.announce(self.n, closed)
        held = await tr.positions()
        for p in held:
            sym = p["symbol"]
            try:
                if not await self._one_position(p):   # a long, or a stop that couldn't be restored (alerted)
                    verified = False
            except SymbolBusy as e:   # another process is acting on it: skip this pass, retry next pass
                verified = False
                await self._position_busy(sym, e)
            except Exception as e:  # noqa: BLE001 - isolate: other positions still get checked
                verified = False   # swallowed here, never in the heartbeat
                await self._position_failed(sym, e)
            else:
                r = self.pos_streak.ok(sym)
                if r:
                    await send_safe(self.n, "guardian_recovered:position", f"position check OK again after {r[0]} "
                                    f"failures ({r[1] / 60:.0f} min)", sym, cooldown_s=0)
        await self.margin_pass(live, held)   # after the stops, never instead of them (guardian_margin.py, #64)
        if verified:   # before the income read: an income outage never stalls it (#16); margin findings only alert
            self._heartbeat(t0)
        self.pos_streak.forget(set(self.pos_streak.fails) - {p["symbol"] for p in held})   # closed meanwhile
        self.untracked_since = {s: t for s, t in self.untracked_since.items() if s in {p["symbol"] for p in held}}
        acct = await self._day_pnl_read()   # after the stops: the pass's one income read
        if acct is None:
            return
        day_pnl = acct["day_pnl"]   # the definition every consumer reads (trading.account, equity_marks.py)
        track_day_peak(self.j, day_pnl)   # arms the give-back stop (governor.py)
        # effective limit, sticky; a human clear holds it (killswitch_rearm.py); the reminder follows the trip exactly
        hit = await guarded("kill_switch", ks.trip_on_daily_loss(
            self.j, self.app.settings, acct["equity"], day_pnl,
            {k: acct[k] for k in ("old_day_pnl", "late", "pending", "degraded")}), self.n)
        await self._daily_loss_alert(day_pnl, hit)
        track_day_low(self.j, day_pnl)   # after the trip: the day's intraday low, for the go-live gate (#87)

    async def _day_pnl_read(self) -> dict | None:
        """trader.account() with fresh positions (a plan close in this pass is in income already); None when it can't
        be read. The job still succeeds, since its stops were checked (the heartbeat means "protection last worked"),
        so an income outage never becomes a stale-heartbeat HALTED. Losses can't be checked meanwhile, so nothing is
        tripped or armed on a guess; after PNL_UNVERIFIED_PASSES in a row: REDUCING, once per outage (no new risk,
        resting entries stay). Sticky like any trip: a human clears it, and that clear holds through the outage."""
        try:
            acct = await asyncio.wait_for(self.app.trader.account(), ACCOUNT_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 (incl. TimeoutError)
            log.warning("day P&L unreadable: %s: %s", type(e).__name__, e)
            self.pnl_streak.fail(PNL_KEY)
            n, since = self.pnl_streak.fails[PNL_KEY], self.pnl_streak.started(PNL_KEY)
            await send_safe(self.n, "day_pnl_unverified", f"day P&L unreadable ({type(e).__name__}: {e}), {n} pass(es) "
                            f"in a row since {since}: the daily-loss trip and give-back peak are NOT checked. Stops "
                            f"are. REDUCING after {PNL_UNVERIFIED_PASSES} passes.", cooldown_s=1800)
            if n >= PNL_UNVERIFIED_PASSES and not self.pnl_tripped:
                self.pnl_tripped = await guarded("kill_switch", ks.trip(
                    self.j, self.app.settings, REDUCING, "guardian", f"day P&L unverified: income unreadable for {n} "
                    f"guardian passes since {since} ({type(e).__name__}): no new risk while losses can't be checked",
                    {"passes": n, "since": since, "error": f"{type(e).__name__}: {e}"}, cause=PNL_UNVERIFIED),
                    self.n) is not None
            return None
        r, tripped = self.pnl_streak.ok(PNL_KEY), self.pnl_tripped
        self.pnl_tripped = False
        if r:
            await send_safe(self.n, "day_pnl_verified", f"day P&L readable again after {r[0]} failed passes "
                            f"({r[1] / 60:.0f} min)" + (": the 'day P&L unverified' REDUCING stays until a human "
                                                         "clears it" if tripped else ""), cooldown_s=0)
        return acct

    async def _daily_loss_alert(self, day_pnl: float, tripped: dict | None) -> None:
        """Reminder while the day's loss is past the limit. It fires exactly when ks.trip_on_daily_loss returned (it
        returns None below the limit) on the day_pnl the trip got, so the two never disagree on basis or limit."""
        if tripped:
            st = ks.read(self.j)
            await self.n.send("daily_loss_limit", f"Day P&L {day_pnl:+.2f} USDT is past the daily loss limit. Kill "
                              f"switch {st['state']} ({st.get('reason')}): sticky, past midnight too, until a human "
                              "clears it. Review open positions.", urgent=True, cooldown_s=6 * 3600)

    async def _position_failed(self, sym: str, e: Exception) -> None:
        """One symbol's failure stays out of the pass result, but a streak of them escalates (ONDO, -1021)."""
        escalate = self.pos_streak.fail(sym)
        if errors.classify(e) is errors.ClockDrift:
            try:
                await self.ex.resync_time()   # the lane-level resync never sees errors swallowed here
            except Exception:  # noqa: BLE001 - logged; the failure itself is alerted just below
                log.exception("time resync failed")
        log.error("position %s failed", sym, exc_info=e)
        await send_safe(self.n, "position_check_error", f"{type(e).__name__}: {e}", sym, cooldown_s=1800)
        if escalate:
            await send_safe(self.n, "guardian_failing:position", f"check failed {self.pos_streak.after}x in a row "
                            f"since {self.pos_streak.started(sym)} (last: {type(e).__name__}: {e}). Its stop and plan "
                            "are NOT being verified: check it on the exchange.", sym, urgent=True, cooldown_s=0)

    async def _position_busy(self, sym: str, e: SymbolBusy) -> None:
        """A session holds the symbol's lock (symlock.py; the guardian waits GUARDIAN_WAIT_S at most, so one busy
        symbol never stalls the others). Not a failure: only a symbol that stays busy pass after pass escalates, and
        only scheduled passes count: a pass a user-data event ran early (#54) never makes it escalate sooner."""
        log.info("position %s skipped this pass: %s", sym, e)
        if self._triggered("positions"):
            return
        if self.pos_streak.fail(sym):
            await send_safe(self.n, "guardian_failing:position", f"busy {self.pos_streak.after} passes in a row since "
                            f"{self.pos_streak.started(sym)} ({e}). Its stop and plan are NOT being verified: check "
                            "what holds it (a hung or suspended session?).", sym, urgent=True, cooldown_s=0)

    async def _one_position(self, p: dict) -> bool:
        """Check one position: stop, reconciliation, plan, funding. True = its check completed with a stop confirmed
        resting (the heartbeat's "verified"); a raise is a failed check, which isn't verified either."""
        sym = p["symbol"]
        if p["side"] != "short":
            await self.n.send("unexpected_long", f"A LONG position exists ({p['contracts']}). This desk is "
                              "short-only; close it manually.", sym, urgent=True)
            await guarded("kill_switch", ks.trip(self.j, self.app.settings, REDUCING, "guardian", f"a {p['side']} "
                          f"position ({p['contracts']}) in {sym} on a short-only desk", {"symbol": sym},
                          cause="long_position"), self.n)
            return False   # nothing here protects a long
        if p.get("error"):   # its stops couldn't be read (Trader.positions): a failed check of this symbol only
            raise RuntimeError(p["error"])
        close, bridges = await stops.listed(self.app.trader, sym)   # a pre-arm or bridge covers a fixed size only
        if p["UNPROTECTED"] or not close or bridges:
            restored = await self._restore_stop(sym)
            if p["UNPROTECTED"] or not close:
                return restored
        else:   # seen resting: the failure count is of consecutive restores (KITE #22, 2026-10-06: a listing gap)
            self.stop_fails.pop(sym, None)
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
        return True

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
            if age >= UNTRACKED_GRACE_S:   # alerted only now: an early alert would arm a 24 h cooldown on a false alarm
                seen = (self.user_stream.last_foreign.get(sym) if self.user_stream else None)
                await send_safe(self.n, "untracked_position", "Position not opened via shortbot: stop is enforced but "
                                "no plan/thesis. Close it, or journal it (adopt: demo desk only)." + (
                                    f" The user-data stream saw an order not placed by shortbot on it: {seen}."
                                    if seen else ""), sym, cooldown_s=86400)
                await ks.trip(self.j, self.app.settings, REDUCING, "guardian", f"reconciliation: {sym} position "
                              f"({p['contracts']}) not in the journal for {age / 60:.0f} min",
                              {"symbol": sym, "contracts": p["contracts"]}, cause="reconciliation")
        except Exception as e:  # noqa: BLE001 - a kill-switch hook never costs the stop/plan checks below
            log.exception("untracked trip %s failed", sym)
            await send_safe(self.n, "guardian_error:kill_switch", f"{type(e).__name__}: {e}", sym, cooldown_s=1800)

    async def _restore_stop(self, sym: str) -> bool:
        """True when a stop rests now (restored, or one was there) or the position is gone; False when it failed."""
        try:
            res = await self.gw.submit(Intent("restore_stop", {"symbol": sym, "fallback_pct": FALLBACK_STOP_PCT}, sym))
            self.stop_fails.pop(sym, None)
            if res and res.get("action") == "bridges_swept":   # a dead stop swap's leftovers (stops.py)
                await self.n.send("bridge_swept", f"leftover stop-swap bridge(s) cancelled: {res}", sym, cooldown_s=0)
            elif res:
                await self.n.send("stop_missing", f"No closePosition stop found. Action: {res}", sym, urgent=True,
                                  cooldown_s=300)
            return True
        except SymbolBusy:
            raise   # someone else is acting on it: not a failed restore (check_positions skips it this pass)
        except Exception as e:  # noqa: BLE001
            n = self.stop_fails[sym] = self.stop_fails.get(sym, 0) + 1
            if n >= MAX_STOP_RESTORE_FAILS:
                r = await self.gw.submit(Intent("force_close", {"symbol": sym, "failures": n}, sym))
                self.stop_fails.pop(sym, None)
                await self.n.send("forced_close", f"Could not restore a stop {n}x ({e}); " + (
                    "a stop is in place now (set elsewhere): NOT closed." if r["covered"] else "position CLOSED."
                    if r["closed"] else "the position was already gone (closed elsewhere): nothing to close."), sym,
                                  urgent=True, cooldown_s=0)
                return True
            await self.n.send("stop_restore_failed", f"attempt {n}/{MAX_STOP_RESTORE_FAILS}: {e}", sym,
                              urgent=True, cooldown_s=0)
            return False

    async def _track_funding(self, sym: str) -> None:
        """Funding paid so far on the open trade (every 15 min), so the plan's funding budget can act on it."""
        trade = self.j.active_trade(sym)
        if not trade or not trade.get("opened_at") or not self.ex.trade:
            return
        if time.time() - self._fund_ts.get(sym, 0) < 900:
            return
        self._fund_ts[sym] = time.time()
        since = int(datetime.fromisoformat(trade["opened_at"]).timestamp() * 1000)
        rows = reconcile.unique_income(await self.ex.trade.fapiPrivateGetIncome({  # each row once (#46)
            "symbol": to_raw(sym), "incomeType": "FUNDING_FEE", "startTime": since, "limit": 1000}))
        self.j.post(Journal.update_trade, trade["id"], funding=round(sum(float(r.get("income") or 0) for r in rows), 6))

    # ------------------------------------------------------------ limit entries (protective, 5 s)
    async def check_entries(self) -> None:
        state = await self._kill_watch()
        if not self.ex.trade:
            return
        if state == HALTED:   # every pass: catches entries a session placed before it saw the trip
            await entry_gates.halt_sweep(self.entries, self.n)
        else:   # a governor halt or a market pause withdraws resting entries too (#12)
            await entry_gates.guardian_sweep(self.entries, self.n)
        for ev in await self.entries.poll():   # pending rows and sends of unknown outcome (#60)
            self._expect_on_stream(ev)
            if ev["event"] in ("cancelled", "expired"):
                await self.n.send(f"entry_{ev['event']}", f"entry #{ev['entry_id']}: {ev.get('why', '')}",
                                  cooldown_s=0)
            elif ev["event"] in ("filled", "tranche_joined", "sibling_joined", "increment") and \
                    state not in (None, ACTIVE):   # (increment: a partly filled entry's rest, entries_partial.py)
                await send_safe(self.n, "fill_while_" + state.lower(), f"entry #{ev['entry_id']} filled while the "
                                f"kill switch is {state}: protected and journalled as trade #{ev.get('trade_id')}. "
                                "Manage it under its plan or close it; it is never auto-closed.", urgent=True,
                                cooldown_s=0)

    def _expect_on_stream(self, ev: dict) -> None:
        """A fill the poll found: the user-data stream should have reported it (a miss counts against its health)."""
        if not self.user_stream or ev["event"] not in ("filled", "tranche_joined", "sibling_joined"):
            return
        row = (self.entries._rows("id=?", (ev["entry_id"],)) or [{}])[0]
        if row.get("client_id") and row.get("filled_at"):
            self.user_stream.expect(row["client_id"], int(datetime.fromisoformat(row["filled_at"]).timestamp() * 1000))

    async def _kill_watch(self) -> str | None:
        """Each 5 s tick: lift a stale-heartbeat halt once protection works again (this guardian's own heartbeat),
        and announce every loosening. That is the tripwire: a clear that bypassed shortbot-admin still reaches the
        human, also one made while the guardian was down (the last state seen is kept in kv). Returns the state, or
        None if it can't be read (the fill poll runs regardless)."""
        try:
            await ks.auto_clear_stale(self.j, self.app.settings)
            st, started = ks.read(self.j), self.kill_seen is None
            seen = self._last_seen() if started else self.kill_seen
            self.kill_seen = st
            if (seen or {}).get("state") != st["state"] or (seen or {}).get("at") != st.get("at"):
                self.j.post(Journal.set, self.j.mode_key(SEEN_KEY),
                            json.dumps({k: st.get(k) for k in ("state", "by", "reason", "at", "cause")}))
            if seen and st["level"] < seen["level"]:
                sanctioned = (st.get("cause") == "auto_clear" or bool(st.get("restored_at"))   # stale halt lifted
                              or str(st.get("by")).startswith("human:"))
                await send_safe(self.n, "kill_switch_loosened", f"{seen['state']} -> {st['state']} by {st.get('by')}: "
                                f"{st.get('reason')}" + (" (while the guardian was down)" if started else "")
                                + ("" if sanctioned else ". NOT via shortbot-admin: check who changed it"),
                                urgent=not sanctioned, cooldown_s=0)
            elif started and st["state"] != ACTIVE:
                await send_safe(self.n, "kill_switch_state", f"guardian started with the kill switch {st['state']} "
                                f"since {st.get('at')} ({st.get('by')}: {st.get('reason')})", cooldown_s=0)
            return st["state"]
        except Exception as e:  # noqa: BLE001 - never fail the protective pass (it would stall the heartbeat)
            log.exception("kill-switch watch failed")
            await send_safe(self.n, "guardian_error:kill_switch", f"{type(e).__name__}: {e}", cooldown_s=1800)
            return None

    def _last_seen(self) -> dict | None:
        """The state the previous guardian run last saw; unreadable = never seen."""
        st = ks._parse(self.j.get(self.j.mode_key(SEEN_KEY)))
        return {**st, "level": ks.LEVEL[st["state"]]} if st else None

    async def reconcile_entries(self) -> None:
        """Entry sends whose outcome the journal doesn't know, resolved by client id (entry_intents.py; it alerts);
        intents whose process died mid-run, ended and alerted with the orders they sent; then every intent of unknown
        outcome, these included, settled by its orders' client ids (resolver.py; it alerts each resolution)."""
        if self.ex.trade:
            await entry_intents.reconcile(self.entries)
        for r in await self.j.write(intents.sweep):
            msg, urgent = resolver.swept(r)
            await send_safe(self.n, "intent_interrupted", msg, r["symbol"], urgent=urgent, cooldown_s=0)
        if self.ex.trade:
            await resolver.resolve(self.app.trader)


# ------------------------------------------------------------ intent handlers (gateway.py): ctx = the guardian
@handler("restore_stop", late_ok=True, ctx="guardian")
async def restore_stop(g, symbol: str, fallback_pct: float) -> dict | None:
    """RestoreStop (GATEWAY.md §4.2): Trader.ensure_stop, which re-reads everything under the symbol lock."""
    return await g.app.trader.ensure_stop(symbol, fallback_pct)


@handler("force_close", late_ok=True, ctx="guardian")
async def force_close(g, symbol: str, failures: int) -> dict:
    """ForceClose: a short whose stop could not be restored `failures` times is closed at market, unless a stop rests
    by now or it is gone (re-read under the lock: another process may have protected or closed it)."""
    tr = g.app.trader
    async with tr.symbol_lock(symbol, "guardian: forced close (no stop)"):
        pos = await tr.position(symbol)
        covered = bool(pos) and bool(await tr.stop_orders(symbol, close_position_only=True))
        if pos and not covered and (await tr.close_market(symbol, float(pos["contracts"]))).get("rejected"):
            pos = None   # -2022: flat before the close landed (closed elsewhere first)
    return {"ok": True, "symbol": symbol, "covered": covered, "closed": bool(pos) and not covered, "gone": not pos}

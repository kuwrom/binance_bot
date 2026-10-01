"""Limit-entry placement (mixed into EntryDesk, entries.py): validate and size each proposal from the current risk
budget, journal the entry row (status 'placing', with its client order id), then place the resting GTD sell
(+ optional pre-armed stop) and journal the outcome (entry_intents.py). A split entry's legs are placed far leg
first and rolled back as one unit if any leg fails.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from . import clientid, killswitch, warmup
from .entry_intents import HINTS, ON_BOOK, SendUnknown
from .exchange import to_symbol
from .plans import build_plan, validate_plan
from .setups import resolve

MIN_TTL_S = 660


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EntryPlacement:
    """Uses EntryDesk's trader, journal, regime, entry-row storage and cancel (_cancel_entry)."""

    async def propose_group(self, proposals: list[dict[str, Any]], ttl_hours: float = 6.0,
                            prearm_stop: bool = True, warmup_override: str | None = None) -> dict[str, Any]:
        """proposals: [{symbol, limit_price, stop_price, setup, thesis, plan?, leverage?, level_type, level_reason,
        cancel_below?, conviction?, size_mult? (0.1-1, only shrinks), tranches? [{limit_price, share}] (split entry:
        2 legs, one stop, one budget; see tranches.py)}]. Sizes from the current risk budget at each limit price.
        First fill cancels the other proposals (a split entry's own legs stay live)."""
        tr = self.tr
        if not 1 <= len(proposals) <= 5:
            return {"ok": False, "error": "propose 1-5 entries per group"}
        if blocked := await killswitch.gate(tr, "propose_entries"):
            return blocked
        cold = warmup.gate(self.j)
        if cold and not (warmup_override and warmup_override.strip()):
            return {"ok": False, "vetoed": [cold], "hint": "or pass warmup_override='<reason>'"}
        await tr.ex.trade_markets()
        regime = await self._regime()
        gid = uuid.uuid4().hex[:8]
        expires_ms = int((time.time() + max(ttl_hours * 3600, MIN_TTL_S)) * 1000)
        placed, rejected, unknown, warnings = [], [], [], []
        for p in proposals:
            try:
                if p.get("tranches"):
                    from . import tranches
                    _, legs = tranches.expand(p)
                    # far leg first: the near leg can fill the moment it is placed (MOVR 2026-10-01)
                    legs = sorted(legs, key=lambda leg: -float(leg["limit_price"]))
                    got: list[dict] = []
                    try:
                        for leg in legs:
                            got.append(await self._place_one(gid, leg, regime, expires_ms, prearm_stop))
                        pinned = tranches.finalize(self.j.db, got)
                    except Exception as e:
                        await self._rollback(got)   # a split entry is one unit (0G 2026-10-01)
                        if isinstance(e, SendUnknown):   # that leg is abandoned: cancelled if it turns up
                            raise ValueError(f"split entry rolled back: a leg's {e}") from e
                        raise
                    placed += [{**x, **pinned} for x in got]
                else:
                    placed.append(await self._place_one(gid, p, regime, expires_ms, prearm_stop))
            except SendUnknown as e:   # may rest on the book: resolved by its client id, never by resending
                unknown.append({"symbol": p.get("symbol"), "entry_id": e.entry_id, "error": str(e),
                                "hint": HINTS["unknown"]})
            except Exception as e:  # noqa: BLE001 - one bad proposal must not block the others
                rejected.append({"symbol": p.get("symbol"), "error": f"{type(e).__name__}: {e}"})
        if placed or unknown:   # bookkeeping: a failure here must not hide what rests on the book
            try:
                self.j.log_decision("propose_entries", f"group {gid}: {len(placed)} limit shorts", None, snapshot={
                    "group": gid, "placed": placed, "rejected": rejected, "unknown": unknown, "regime": regime})
            except Exception as e:  # noqa: BLE001
                warnings.append(f"decision not journalled: {type(e).__name__}: {e}")
            try:
                await tr.alert("action_propose", f"group {gid}: " + ", ".join(
                    [f"{x['symbol'].split('/')[0]} sell {x['limit_price']} stop {x['stop_price']}" for x in placed]
                    + [f"{x['symbol']} entry #{x['entry_id']} send status UNKNOWN" for x in unknown]), None, False)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"alert not sent: {type(e).__name__}: {e}")
        return {"ok": bool(placed), "group_id": gid, "placed": placed, "rejected": rejected, "unknown": unknown,
                **({"warnings": warnings} if warnings else {})}

    async def _place_one(self, gid: str, p: dict, regime: str, expires_ms: int, prearm: bool) -> dict[str, Any]:
        tr = self.tr
        sym = to_symbol(p["symbol"])
        if sym not in tr.t.markets:
            raise ValueError(f"{sym} not listed on the connected account")
        st = resolve(p["setup"])
        limit, stop = float(p["limit_price"]), float(p["stop_price"])
        tk = await tr.t.fetch_ticker(sym)
        last = float(tk["last"])
        if limit <= last:
            raise ValueError(f"limit {limit} must be ABOVE the market {last} for a resting short (else enter now)")
        # the same signal vetoes as a market entry, checked at placement (a painted rejection, a squeeze regime...):
        # a resting order must not be placed on a read the tape already contradicts (QNT 2026-09-30)
        checks = await tr.prechecks(tr.ex, sym, await tr.ex.public.fetch_ticker(sym))
        vetoes = list(checks.get("vetoes") or [])
        if vetoes and not str(p.get("veto_override") or "").strip():
            raise ValueError(f"vetoed at placement: {vetoes} (pass veto_override='<research reason>' to override)")
        if stop <= limit:
            raise ValueError("stop must be above the limit price")
        plan = build_plan(st.default_plan, p.get("plan"))
        errs = validate_plan(plan, limit, stop)
        if errs:
            raise ValueError("; ".join(errs))
        budget = await tr.budget(tr, sym, regime, p.get("conviction", "B"), p["setup"])
        if budget["blocked"]:
            raise ValueError(f"risk budget blocked: {budget['reasons']}")
        dist = (stop - limit) / limit
        lim = tr.ex.limits
        size_mult = max(0.1, min(1.0, float(p.get("size_mult", 1.0))))   # e.g. read's thin-float note: 0.25-0.5
        from .risk import liquidity_tier
        oi = await tr.ex.oi_history(sym, "1h", 1) if hasattr(tr.ex, "oi_history") else []
        tier, lmult = liquidity_tier(float((await tr.ex.public.fetch_ticker(sym)).get("quoteVolume") or 0),
                                     oi[-1]["oi_value"] if oi else None)
        if tier is None:
            raise ValueError("too thin to short: below liquidity tier B (24h vol >= $5M and OI >= $10M)")
        size_mult = min(size_mult, lmult) if lmult < 1 else size_mult   # tier B = half risk
        from .risk import funding_mult
        rate = iv = None   # shorts paying funding (negative / 1h interval): half size; each lookup on its own
        try:
            rate = (await tr.ex.public.fetch_funding_rate(sym)).get("fundingRate")
        except Exception:  # noqa: BLE001
            pass
        try:
            ivs = await tr.ex.funding_intervals() if hasattr(tr.ex, "funding_intervals") else {}
            iv = (ivs.get(sym.split("/")[0] + "USDT") or {}).get("interval_h")
        except Exception:  # noqa: BLE001
            pass
        fm = funding_mult(rate, iv)
        size_mult = min(size_mult, fm)
        share = float(p.get("risk_share", 1.0))   # a split-entry leg risks its share of ONE budget
        notional = min(budget["risk_usd"] * size_mult * share / dist, lim.max_position_notional * share) * 0.98
        amount = float(tr.t.amount_to_precision(sym, notional / limit))
        min_cost = tr.t.market(sym)["limits"]["cost"]["min"] or 0
        if amount * limit < min_cost:
            raise ValueError(f"size {amount * limit:.2f} below exchange minimum {min_cost}")
        lev = int(p.get("leverage") or 3)
        from .risk import est_liquidation_distance_pct
        if dist * 100 > est_liquidation_distance_pct(lev) - lim.liq_buffer_pct:
            raise ValueError(f"at {lev}x the stop ({dist * 100:.1f}%) sits beyond liquidation minus buffer")
        px = float(tr.t.price_to_precision(sym, limit))
        from .risk import ShortRequest, check_open_short
        acct = await tr.account_state()
        # other groups' resting (or possibly resting: placing/unknown) orders count as exposure: one OCO order per
        # group can fill, but a split entry's legs can ALL fill, so a group's exposure = its biggest single order or
        # its biggest whole tranche. Each external order (not ours) can fill on its own: one group each.
        by_group: dict[str, list[dict]] = {}
        for r in self._rows(ON_BOOK):
            g = f"external{r['id']}" if r["status"] == "external" else r["group_id"]
            if g != gid:
                by_group.setdefault(g, []).append(r)
        worst: dict[str, tuple[str, float]] = {}
        for g, rows in by_group.items():
            units: dict[str, float] = {}
            for r in rows:
                key = r.get("tranche") or f"single{r['id']}"
                units[key] = units.get(key, 0.0) + r["amount"] * r["limit_price"]
            k = max(units, key=units.get)
            worst[g] = (next(r["symbol"] for r in rows if (r.get("tranche") or f"single{r['id']}") == k), units[k])
        acct = type(acct)(acct.equity, acct.open_symbols + tuple(s for s, _ in worst.values()),
                          acct.total_open_notional + sum(n for _, n in worst.values()), acct.day_pnl)
        prod = await tr.ex.public.fetch_ticker(sym)
        check_open_short(ShortRequest(sym, amount * px, px, stop, lev, float(prod.get("quoteVolume") or 0)), acct, lim)
        c1h = await tr.ex.public.fetch_ohlcv(sym, "1h", limit=30)   # every read comes BEFORE the intent: a failure
        from .indicators import atr                                   # up to here leaves nothing on the exchange
        a = atr(c1h) or 0.0
        if blocked := await killswitch.gate(tr, "limit entry"):   # a trip while this group was being placed: no row
            raise ValueError(blocked["error"])
        async with tr.symbol_lock(sym, f"place a limit entry (group {gid})"):
            if blocked := killswitch.state_refusal(tr.j, "limit entry"):   # tripped while we waited for the lock
                raise ValueError(blocked["error"])
            if await tr.position(sym):   # filled meanwhile (e.g. a split's far leg): never re-lever a live position
                raise ValueError(f"already short {sym} (opened since the checks); manage the existing position")
            await tr._prepare(sym, lev)   # leverage + isolated margin: only on a flat symbol, after the risk check
            # write-ahead intent (entry_intents.py): the row and its client id exist before the order is sent
            cid = clientid.new("entry", gid)
            eid = self.j._insert(
                "INSERT INTO entries (group_id, symbol, status, created_at, expires_ms, limit_price, stop_price,"
                " amount, leverage, setup, thesis, plan, level_type, level_reason, cancel_below, atr_1h, ref_price,"
                " ref_ms, conviction, regime_cell, tranche, leg, client_id, abandon)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (gid, sym, "placing", _now_iso(), expires_ms, px, stop, amount, lev, p["setup"], p["thesis"],
                 json.dumps(plan.to_dict()), p.get("level_type", "unspecified"), p.get("level_reason", ""),
                 p.get("cancel_below"), a, last, int(time.time() * 1000), budget.get("conviction"),
                 budget.get("cell"), p.get("tranche"), p.get("leg"), cid,
                 1 if p.get("tranche") else 0))   # a split leg is withdrawn if unsure
            try:
                order = await tr.t.create_order(sym, "limit", "sell", amount, px, {
                    "timeInForce": "GTD", "goodTillDate": expires_ms, "clientOrderId": cid})
            except Exception as e:  # noqa: BLE001 - journal the outcome first: refused, or unknown (may rest)
                unsure = clientid.send_unknown(e)
                self._settle(eid, "placing", status="unknown" if unsure else "rejected",
                             reason=f"{type(e).__name__}: {e}")
                if unsure:
                    raise SendUnknown(eid, e) from e
                raise
            if not self._settle(eid, "placing", status="pending", order_id=order.get("id")):
                raise RuntimeError(f"entry #{eid} was resolved by the reconciler while its order was in flight")
            prearm_id = await self._prearm(eid, sym, amount, stop) if prearm else None
        odds = {}
        try:
            from .probability import ProbabilityEngine
            tgts = [t.price for t in plan.targets] or [px * (1 - 2 * dist)]
            pe = await ProbabilityEngine(tr.ex, self.j).short_bracket(
                sym, stop, tgts[:1], float(plan.time_stop_hours or 24), entry=px,
                fill_hours=(expires_ms / 1000 - time.time()) / 3600, log_source="entry", ref_id=eid,
                context={"setup": p["setup"], "level_type": p.get("level_type"), "regime": regime})
            if pe.get("ok"):
                odds = {"p_fill_upper_bound": pe.get("fill_prob"), "p_target_first_if_filled": pe["targets"][0]["p_target"],
                        "odds_label": pe["label"]}
        except Exception:  # noqa: BLE001 - odds are informational; never block placement
            pass
        return {**odds, "entry_id": eid, "symbol": sym, "limit_price": px, "stop_price": stop, "amount": amount,
                "risk_usdt": round(amount * (stop - px), 2), "leverage": lev, "prearmed_stop": bool(prearm_id),
                "client_id": cid,
                "expires": datetime.fromtimestamp(expires_ms / 1000, timezone.utc).isoformat(timespec="minutes")}

    async def _prearm(self, eid: int, sym: str, amount: float, stop: float) -> str | None:
        """Reduce-only stop resting from the first fill instant, journalled like its limit: client id first, then the
        send. A lost answer leaves it unresolved (prearm_cid set, prearm_id NULL) for entry_intents. Never raises: the
        limit already rests and is tracked, and the 5 s fill poll still protects."""
        t, cid = self.tr.t, clientid.new("prearm", eid)
        try:
            self._update(eid, prearm_cid=cid)
        except sqlite3.Error:
            return None   # no intent journalled: don't send
        o, refused = None, False
        try:
            o = await t.create_order(sym, "STOP_MARKET", "buy", amount, None, {
                "triggerPrice": float(t.price_to_precision(sym, stop)), "reduceOnly": True,
                "workingType": "MARK_PRICE", "clientOrderId": cid})
        except Exception as e:  # noqa: BLE001
            refused = not clientid.send_unknown(e)
        try:
            if o is not None:
                self._update(eid, prearm_id=o.get("id"))
            elif refused:
                self._update(eid, prearm_cid=None)   # nothing rests
        except sqlite3.Error:
            pass   # the row keeps prearm_cid: entry_intents finds the stop by it
        return (o or {}).get("id")

    async def _rollback(self, got: list[dict]) -> None:
        """Undo a split entry's placed legs after a later leg failed. Cancel each limit FIRST: only a leg cancelled
        unfilled loses its pre-armed stop. A leg that (partly) filled keeps its stop and stays pending, pinned to its
        own size, so the poll adopts and protects it (MOVR 2026-10-01: a stripped stop left a naked short)."""
        kept = []
        for x in got:
            row = self._rows("id=?", (x["entry_id"],))[0]
            if await self._cancel_entry(row, "cancelled", "split entry rejected: sibling leg failed") != "unfilled":
                kept.append(x)   # (partly) filled, or the cancel failed: keeps its stop, the poll reconciles
        if kept:
            from . import tranches
            tranches.finalize(self.j.db, kept)

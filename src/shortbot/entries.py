"""Limit-entry groups (docs/POSITION_MANAGEMENT.md §c): propose 3-5 shorts at better levels (below a round
number / prior high, or above the stop cluster); the first fill wins and cancels the rest (OCO-style).

Mechanics (Binance USDⓈ-M, verified in docs/ccxt 2026-09):
- entry = limit SELL above the market (rests as maker) with an exchange-side expiry (GTD, ≥ 10 min) so it
  dies even if the guardian is down. GTX (post-only) and GTD are both time-in-force values, so we pick GTD.
- protection: on fill the guardian (5 s poll) places the closePosition stop; optionally a reduce-only stop is
  pre-armed at placement so protection exists from the first fill instant (demo-verify: `prearm_stop`)
- cancel rules: TTL, price ran away below `cancel_below` (move happened without us), price approaching faster
  than 1·ATR(1h) within 15 min (cascade, not a tap), or the analyst cancels. Replace at most once.
- risk: every pending order is sized from the CURRENT risk budget; a group counts once (only one may fill)
Every entry row is research data: fill rate, time-to-fill, level type, and unfilled counterfactuals.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from . import warmup
from .exchange import to_symbol
from .plans import build_plan, validate_plan
from .market.market_map import cached
from .sessions import session_bucket
from .setups import resolve

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, symbol TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, expires_ms INTEGER NOT NULL, limit_price REAL, stop_price REAL, amount REAL,
    leverage INTEGER, setup TEXT, thesis TEXT, plan TEXT, level_type TEXT, level_reason TEXT,
    cancel_below REAL, atr_1h REAL, ref_price REAL, ref_ms INTEGER, order_id TEXT, prearm_id TEXT,
    replaced INTEGER DEFAULT 0, trade_id INTEGER, filled_at TEXT, fill_price REAL, filled_qty REAL, reason TEXT
);
"""
MIN_TTL_S = 660
FAST_APPROACH_WINDOW_MS = 15 * 60 * 1000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EntryDesk:
    def __init__(self, trader, regime_fn=None):
        self.tr = trader
        self.j = trader.j
        self.j.db.executescript(SCHEMA)
        have = {r[1] for r in self.j.db.execute("PRAGMA table_info(entries)")}
        for col, typ in (("conviction", "TEXT"), ("regime_cell", "TEXT"), ("tranche", "TEXT"), ("leg", "INTEGER")):
            if col not in have:
                self.j.db.execute(f"ALTER TABLE entries ADD COLUMN {col} {typ}")
        self._regime_fn = regime_fn

    async def _regime(self) -> str:
        if self._regime_fn:
            return await self._regime_fn()
        from .scanner import market_regime
        return (await market_regime(self.tr.ex))["verdict_for_shorts"]

    # ------------------------------------------------------------ storage
    def _rows(self, where: str = "1=1", args: tuple = ()) -> list[dict[str, Any]]:
        rows = self.j._rows(f"SELECT * FROM entries WHERE {where} ORDER BY id", args)
        for r in rows:
            r["plan"] = json.loads(r["plan"]) if r.get("plan") else {}
        return rows

    def _update(self, eid: int, **f: Any) -> None:
        self.j.db.execute(f"UPDATE entries SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), eid))
        self.j.db.commit()

    def pending(self, group_id: str | None = None) -> list[dict[str, Any]]:
        if group_id:
            return self._rows("status='pending' AND group_id=?", (group_id,))
        return self._rows("status='pending'")

    # ------------------------------------------------------------ propose
    async def propose_group(self, proposals: list[dict[str, Any]], ttl_hours: float = 6.0,
                            prearm_stop: bool = True, warmup_override: str | None = None) -> dict[str, Any]:
        """proposals: [{symbol, limit_price, stop_price, setup, thesis, plan?, leverage?, level_type, level_reason,
        cancel_below?, conviction?, size_mult? (0.1-1, only shrinks), tranches? [{limit_price, share}] (split entry:
        2 legs, one stop, one budget; see tranches.py)}]. Sizes from the current risk budget at each limit price.
        First fill cancels the other proposals (a split entry's own legs stay live)."""
        tr = self.tr
        if not 1 <= len(proposals) <= 5:
            return {"ok": False, "error": "propose 1-5 entries per group"}
        cold = warmup.gate(self.j)
        if cold and not (warmup_override and warmup_override.strip()):
            return {"ok": False, "vetoed": [cold], "hint": "or pass warmup_override='<reason>'"}
        await tr.ex.trade_markets()
        regime = await self._regime()
        gid = uuid.uuid4().hex[:8]
        expires_ms = int((time.time() + max(ttl_hours * 3600, MIN_TTL_S)) * 1000)
        placed, rejected = [], []
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
                    except Exception:
                        await self._rollback(got)   # a split entry is one unit (0G 2026-10-01)
                        raise
                    pinned = tranches.finalize(self.j.db, got)
                    placed += [{**x, **pinned} for x in got]
                else:
                    placed.append(await self._place_one(gid, p, regime, expires_ms, prearm_stop))
            except Exception as e:  # noqa: BLE001 - one bad proposal must not block the others
                rejected.append({"symbol": p.get("symbol"), "error": f"{type(e).__name__}: {e}"})
        if placed:
            self.j.log_decision("propose_entries", f"group {gid}: {len(placed)} limit shorts", None,
                                snapshot={"group": gid, "placed": placed, "rejected": rejected, "regime": regime})
            await tr.alert("action_propose", f"group {gid}: " + ", ".join(
                f"{x['symbol'].split('/')[0]} sell {x['limit_price']} stop {x['stop_price']}" for x in placed), None, False)
        return {"ok": bool(placed), "group_id": gid, "placed": placed, "rejected": rejected}

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
        await tr._prepare(sym, lev)
        px = float(tr.t.price_to_precision(sym, limit))
        from .risk import ShortRequest, check_open_short
        acct = await tr.account_state()
        # other groups' pending orders count as exposure: one OCO order per group can fill, but a split entry's
        # legs can ALL fill, so a group's exposure = its biggest single order or its biggest whole tranche
        by_group: dict[str, list[dict]] = {}
        for r in self.pending():
            if r["group_id"] != gid:
                by_group.setdefault(r["group_id"], []).append(r)
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
        order = await tr.t.create_order(sym, "limit", "sell", amount, px,
                                        {"timeInForce": "GTD", "goodTillDate": expires_ms})
        prearm_id = None
        if prearm:
            try:
                o = await tr.t.create_order(sym, "STOP_MARKET", "buy", amount, None, {
                    "triggerPrice": float(tr.t.price_to_precision(sym, stop)), "reduceOnly": True,
                    "workingType": "MARK_PRICE"})
                prearm_id = o.get("id")
            except Exception:  # noqa: BLE001 - the 5 s fill poll still protects
                prearm_id = None
        c1h = await tr.ex.public.fetch_ohlcv(sym, "1h", limit=30)
        from .indicators import atr
        a = atr(c1h) or 0.0
        cur = self.j.db.execute(
            "INSERT INTO entries (group_id, symbol, status, created_at, expires_ms, limit_price, stop_price, amount,"
            " leverage, setup, thesis, plan, level_type, level_reason, cancel_below, atr_1h, ref_price, ref_ms,"
            " order_id, prearm_id, conviction, regime_cell, tranche, leg)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (gid, sym, "pending", _now_iso(), expires_ms, px, stop, amount, lev, p["setup"], p["thesis"],
             json.dumps(plan.to_dict()), p.get("level_type", "unspecified"), p.get("level_reason", ""),
             p.get("cancel_below"), a, last, int(time.time() * 1000), order.get("id"), prearm_id,
             budget.get("conviction"), budget.get("cell"), p.get("tranche"), p.get("leg")))
        self.j.db.commit()
        odds = {}
        try:
            from .probability import ProbabilityEngine
            tgts = [t.price for t in plan.targets] or [px * (1 - 2 * dist)]
            pe = await ProbabilityEngine(tr.ex, self.j).short_bracket(
                sym, stop, tgts[:1], float(plan.time_stop_hours or 24), entry=px,
                fill_hours=(expires_ms / 1000 - time.time()) / 3600, log_source="entry", ref_id=cur.lastrowid,
                context={"setup": p["setup"], "level_type": p.get("level_type"), "regime": regime})
            if pe.get("ok"):
                odds = {"p_fill_upper_bound": pe.get("fill_prob"), "p_target_first_if_filled": pe["targets"][0]["p_target"],
                        "odds_label": pe["label"]}
        except Exception:  # noqa: BLE001 - odds are informational; never block placement
            pass
        return {**odds, "entry_id": cur.lastrowid, "symbol": sym, "limit_price": px, "stop_price": stop, "amount": amount,
                "risk_usdt": round(amount * (stop - px), 2), "leverage": lev, "prearmed_stop": bool(prearm_id),
                "expires": datetime.fromtimestamp(expires_ms / 1000, timezone.utc).isoformat(timespec="minutes")}

    async def _rollback(self, got: list[dict]) -> None:
        """Undo a split entry's placed legs after a later leg failed. Cancel each limit FIRST: only a leg cancelled
        unfilled loses its pre-armed stop. A leg that (partly) filled keeps its stop and stays pending, pinned to its
        own size, so the poll adopts and protects it (MOVR 2026-10-01: a stripped stop left a naked short)."""
        t, kept = self.tr.t, []
        for x in got:
            row = self._rows("id=?", (x["entry_id"],))[0]
            try:
                o = await t.cancel_order(row["order_id"], row["symbol"]) or {}
            except Exception:  # noqa: BLE001 - filled or unknown: keep its stop, the poll reconciles
                kept.append(x)
                continue
            if float(o.get("filled") or 0) > 0:
                kept.append(x)
                continue
            if row["prearm_id"]:
                try:
                    await t.cancel_order(row["prearm_id"], row["symbol"], params={"trigger": True})
                except Exception:  # noqa: BLE001
                    pass
            self._update(row["id"], status="cancelled", reason="split entry rejected: sibling leg failed")
        if kept:
            from . import tranches
            tranches.finalize(self.j.db, kept)

    # ------------------------------------------------------------ cancel / amend
    async def _cancel_entry(self, e: dict, status: str, reason: str) -> None:
        t = self.tr.t
        for oid, params in ((e["order_id"], {}), (e["prearm_id"], {"trigger": True})):
            if oid:
                try:
                    await t.cancel_order(oid, e["symbol"], params=params)
                except Exception:  # noqa: BLE001 - already gone (filled/expired) is fine
                    pass
        self._update(e["id"], status=status, reason=reason)

    async def cancel_group(self, group_id: str, reason: str, keep: int | None = None,
                           keep_tranche: str | None = None) -> dict[str, Any]:
        n = 0
        for e in self.pending(group_id):
            if e["id"] != keep and not (keep_tranche and e.get("tranche") == keep_tranche):
                await self._cancel_entry(e, "cancelled", reason)
                n += 1
        return {"ok": True, "cancelled": n}

    async def amend(self, entry_id: int, new_limit: float, reason: str) -> dict[str, Any]:
        rows = self._rows("id=? AND status='pending'", (entry_id,))
        if not rows:
            return {"ok": False, "error": "no pending entry with that id"}
        e = rows[0]
        if e["replaced"]:
            return {"ok": False, "error": "already replaced once (rule: replace at most once, don't chase)"}
        if new_limit >= e["stop_price"]:
            return {"ok": False, "error": "new limit must stay below the stop"}
        t = self.tr.t
        px = float(t.price_to_precision(e["symbol"], new_limit))
        await t.edit_order(e["order_id"], e["symbol"], "limit", "sell", e["amount"], px)
        self._update(e["id"], limit_price=px, replaced=1, reason=f"amended: {reason}")
        return {"ok": True, "entry_id": entry_id, "new_limit": px}

    # ------------------------------------------------------------ the 5 s loop (guardian)
    async def poll(self) -> list[dict[str, Any]]:
        """Detect fills (protect + journal + cancel siblings), expiries, and cancel conditions."""
        events: list[dict[str, Any]] = []
        now_ms = int(time.time() * 1000)
        for e in self.pending():
            if not self._rows("id=? AND status='pending'", (e["id"],)):
                continue   # a sibling filled earlier in this pass and cancelled it: keep that status and reason
            if e.get("tranche"):
                from .tranches import parent_closed
                o = await self.tr.t.fetch_order(e["order_id"], e["symbol"])
                if parent_closed(self, e) and not float(o.get("filled") or 0):
                    await self._cancel_entry(e, "cancelled", "tranche trade already closed")
                    events.append({"entry_id": e["id"], "event": "cancelled", "why": "tranche trade closed"})
                    continue
            try:
                o = await self.tr.t.fetch_order(e["order_id"], e["symbol"])
            except Exception:  # noqa: BLE001
                continue
            filled = float(o.get("filled") or 0)
            if filled > 0:
                events.append(await self._on_fill(e, o))
            elif o.get("status") in ("canceled", "expired", "rejected") or now_ms > e["expires_ms"]:
                await self._cancel_entry(e, "expired", f"exchange status {o.get('status')}")
                events.append({"entry_id": e["id"], "event": "expired"})
            else:
                why = await self._should_cancel(e, now_ms)
                if why:
                    await self._cancel_entry(e, "cancelled", why)
                    events.append({"entry_id": e["id"], "event": "cancelled", "why": why})
        return events

    async def _should_cancel(self, e: dict, now_ms: int) -> str | None:
        last = float((await self.tr.ex.public.fetch_ticker(e["symbol"]))["last"])
        if e["cancel_below"] and last <= e["cancel_below"]:
            return f"price {last} fell below {e['cancel_below']} without filling: the move happened without us"
        if e["atr_1h"] and now_ms - e["ref_ms"] <= FAST_APPROACH_WINDOW_MS and last - e["ref_price"] >= e["atr_1h"]:
            return f"price rose {last - e['ref_price']:.6g} (≥ 1 ATR) toward the limit within 15 min: cascade risk"
        if now_ms - e["ref_ms"] > FAST_APPROACH_WINDOW_MS:
            self._update(e["id"], ref_price=last, ref_ms=now_ms)  # rolling 15-min reference
        return None

    async def _on_fill(self, e: dict, o: dict) -> dict[str, Any]:
        tr = self.tr
        sym = e["symbol"]
        if o.get("status") != "closed":  # partial: cancel the rest, protect what we have
            try:
                await tr.t.cancel_order(e["order_id"], sym)
            except Exception:  # noqa: BLE001
                pass
        async with tr.lock:
            pos = await tr._verified_position(sym)
            if not pos:
                self._update(e["id"], status="error", reason="fill reported but no position found")
                return {"entry_id": e["id"], "event": "error"}
            if e["prearm_id"]:
                try:
                    await tr.t.cancel_order(e["prearm_id"], sym, params={"trigger": True})
                except Exception:  # noqa: BLE001
                    pass
            cps = await tr.stop_orders(sym, close_position_only=True)
            if not cps:   # a pre-armed reduce-only stop (another leg / order) is NOT whole-position protection
                await tr._place_stop(sym, e["stop_price"], tries=3)
        if cps and min(c["trigger"] for c in cps) > e["stop_price"] * 1.0001:
            # e.g. the guardian's +10% fallback stop landed first: tighten it to this entry's plan stop
            await tr.move_stop(sym, e["stop_price"], f"entry #{e['id']} filled: plan stop")
        if e.get("tranche"):
            from . import tranches
            tid = tranches.open_trade_id(self, e)
            if tid:
                return await tranches.join(self, e, pos, tid)
        fill, qty = float(pos["entryPrice"]), float(pos["contracts"])
        tid = self.j.create_trade(sym, e["setup"], e["thesis"], e["stop_price"], e["leverage"], e["plan"],
                                  {"entry_type": "limit", "level_type": e["level_type"], "level_reason": e["level_reason"],
                                   "group_id": e["group_id"], "limit_price": e["limit_price"],
                                   "session_bucket": session_bucket(datetime.now(timezone.utc)),
                                   "breadth_pct": ((cached(self.j, 1800) or {}).get("header") or {}).get("breadth_pct"),
                                   "placed_session": session_bucket(datetime.fromisoformat(e["created_at"]))},
                                  tr.ex.experiment)
        self.j.update_trade(tid, conviction=e.get("conviction"), regime_cell=e.get("regime_cell"))
        self.j.activate_trade(tid, fill, qty, round(fill * qty, 2))
        self._update(e["id"], status="filled", trade_id=tid, filled_at=_now_iso(), fill_price=fill, filled_qty=qty)
        others = await self.cancel_group(e["group_id"], f"sibling #{e['id']} filled", keep=e["id"],
                                         keep_tranche=e.get("tranche"))
        await tr.alert("action_fill", f"limit short filled {qty} @ {fill}, stop {e['stop_price']} placed; "
                       f"{others['cancelled']} sibling order(s) cancelled", sym, False)
        return {"entry_id": e["id"], "event": "filled", "trade_id": tid, "fill": fill, "qty": qty}

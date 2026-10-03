"""Demo-only exchange-behaviour probes (#55): `shortbot probe NAME [--symbol SYM] [--all]`.

The desk's order paths rest on Binance behaviour its fake (tests/fakes.py) encodes from the docs, secondary sources or
inference. Each probe forces one such behaviour on the DEMO account at minimum size and records what Binance answered
next to what the fake assumes. Cases: probe_trades.py (a probe position), probe_cases.py (flat), probe_stream.py (7).
- demo only: settings.mode demo AND the trade client on the demo host; kill switch ACTIVE with a verified guardian
  (killswitch.gate), re-checked inside the lock and before every opening order
- never on a symbol the desk uses: no open journal trade, no entry that is or may be on the book, and the exchange shows
  the symbol clean (no position, order or conditional order); probes 1 and 6 act on the whole symbol
- the whole probe holds the symbol's lock (#21) and sends through Exchanges' trade client (weight class protective,
  #70) with client ids sb-x<number><base>-<nonce> (#9; the open-orders sweep leaves them alone)
- a probe position gets a closePosition safety stop at once, and kv probe_active:<symbol> holds an expiry ACTIVE_TTL_S
  ahead, refreshed while the probe runs and deleted after its cleanup. The guardian treats a live row's position as
  explained (entry_intents.explained_symbols): no sticky reconciliation REDUCING, its stop still enforced. An expired
  row counts as absent, so a crashed probe's leftover trips as any untracked position does
- cleanup in `finally`: countdown off, the probe's regular orders cancelled, the position closed through Trader.close
  (the desk's close path), the probe's conditional orders cancelled, the symbol re-read; anything left alerts urgently
- one `probe_result` decision row per probe: every call with its raw answer or error code, what was seen, the fake's
  assumption and whether it held. A refusal sends and journals nothing (the CLI's own call row records it)
Not probed, as they can't be forced: the 503 body format (#5) and INSURANCE_CLEAR timing (#14)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any, AsyncIterator

from . import killswitch as ks
from . import probe_cases as pc
from . import probe_kit as kit
from . import probe_stream as ps
from . import probe_trades as pt
from .exchange import to_symbol
from .journal import now_iso
from .symlock import SymbolBusy

DEFAULT_SYMBOL = "XRPUSDT"
ACTIVE_KEY = "probe_active:"   # kv: a probe position on <symbol> until <expiry epoch s>
ACTIVE_TTL_S, REFRESH_S = 60.0, 20.0
GAP_S = 3.0                    # between probes under --all
CLEANUP_S = 90.0


@dataclass(frozen=True)
class Case:
    number: str
    position: bool             # holds a probe position (probe_active row, safety stop)
    fn: Callable[[kit.Probe], Awaitable[dict[str, Any]]]
    fake: str                  # what tests/fakes.py assumes, and where
    budget_s: float = 75.0


CASES: dict[str, Case] = {
    "second-close-stop": Case("1", True, pt.second_close_stop, "a closePosition STOP_MARKET while flat is refused "
                              "-4509, the first or a second; with a position a second one -4130 (fakes._new_stop; "
                              "demo 2026-10-02)"),
    "close-stop-at-flat": Case("2", True, pt.close_stop_at_flat, "a closePosition stop leaves with its position: "
                               "algoStatus EXPIRED at flat (fake_engine._fill; demo 2026-10-02)"),
    "cancel-finished": Case("3", True, pt.cancel_finished, "cancelling a conditional order that is no longer open "
                            "(FINISHED, EXPIRED, CANCELED) answers -2011 OrderNotFound (fakes.cancel_order; demo "
                            "2026-10-02)"),
    "reduce-only-oversize": Case("4", True, pt.reduce_only_oversize, "a reduce-only LIMIT or MARKET order larger than "
                                 "the position is capped at it, the LIMIT rests (fake_reduce._ro_compete; demo "
                                 "2026-10-02)"),
    "countdown-cancel-all": Case("6", False, pc.countdown_cancel_all, "countdownCancelAll cancels regular AND "
                                 "conditional orders (fake_engine.advance; demo 2026-10-02)", 60.0),
    "user-stream": Case("7", False, ps.user_stream, "ORDER_TRADE_UPDATE and ALGO_UPDATE reach <host>/private/ws"
                        "?listenKey=K in demo's shapes (tests/fake_stream.py; probe_result 387)", 60.0),
    "trigger-listing-cid": Case("8", False, pc.trigger_listing_cid, "the trigger listing carries our clientOrderId and "
                                "a lookup by clientAlgoId finds it (fakes._algo_view; shape seen on demo 2026-09-27)", 60.0),
    "fetch-by-cid": Case("9", False, pc.fetch_by_cid, "fetch_order by clientOrderId finds a resting and a cancelled "
                         "limit; an unknown id answers -2013 (fakes.fetch_order)", 60.0),
    "income-timestamps": Case("10", False, pc.income_timestamps, "COMMISSION and REALIZED_PNL income rows carry their "
                              "trade's tradeId and its time floored to the second (fake_engine._fill; demo "
                              "2026-10-02)", 60.0),
    "algo-unknown-cid": Case("11", False, pc.algo_unknown_cid, "Query-Algo-Order for an unknown clientAlgoId answers "
                             "-2013 OrderNotFound (fakes.fetch_order; undocumented, clientid.lookup relies on it)", 60.0),
    "reduce-only-stop-at-flat": Case("12b", True, pt.reduce_only_stop_at_flat, "a reduce-only STOP_MARKET (pre-arm "
                                     "shape) stays NEW at flat, rejected if it triggers (fake_engine._fill)"),
    "adl-quantile": Case("13", True, pt.adl_quantile, "adlQuantile?symbol= answers one object {symbol, adlQuantile: "
                         "{LONG, SHORT, BOTH}}; without symbol a list of the symbols with positions (fake_account)"),
}
# flat probes first, the trading ones next, the income read last (it reads their trades)
ORDER = ["algo-unknown-cid", "trigger-listing-cid", "fetch-by-cid", "user-stream", "countdown-cancel-all",
         "second-close-stop", "close-stop-at-flat", "reduce-only-stop-at-flat", "cancel-finished",
         "reduce-only-oversize", "adl-quantile", "income-timestamps"]


# ------------------------------------------------------------ the guardian's view (entry_intents.explained_symbols)
def active_symbols(j, now: float | None = None) -> set[str]:
    """Symbols where a probe position is live: an unexpired probe_active row. Demo journals only; never raises (an
    unreadable row counts as absent, so the reconciliation trip stays armed)."""
    if j.mode != "demo":
        return set()
    now = time.time() if now is None else now
    out = set()
    try:
        for r in j._rows("SELECT k, v FROM kv WHERE k LIKE ?", (ACTIVE_KEY + "%",)):
            with suppress(TypeError, ValueError):
                if r["k"].startswith(ACTIVE_KEY) and float(r["v"]) > now:
                    out.add(r["k"][len(ACTIVE_KEY):])
    except Exception:  # noqa: BLE001 - see above
        return set()
    return out


@asynccontextmanager
async def _active(j, symbol: str, on: bool) -> AsyncIterator[None]:
    if not on:
        yield
        return
    key = ACTIVE_KEY + symbol

    def stamp() -> None:
        j.set(key, str(time.time() + ACTIVE_TTL_S))

    async def refresh() -> None:
        while True:
            await asyncio.sleep(REFRESH_S)   # real time: the guardian reads the row against the wall clock
            stamp()
    stamp()
    task = asyncio.create_task(refresh())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        j.db.execute("DELETE FROM kv WHERE k=?", (key,))
        j.db.commit()


# ------------------------------------------------------------ refusals
async def _outside(tr, sym: str, name: str) -> str | None:
    s, t = tr.ex.settings, tr.ex.trade
    if s.mode != "demo" or s.is_live:
        return f"probes run in demo only (mode {s.mode})"
    if t is None:
        return "no demo trading keys"
    host = str(((getattr(t, "urls", None) or {}).get("api") or {}).get("fapiPrivate") or "")
    if "demo-fapi" not in host:
        return f"the trade client is not on the demo host ({host or 'unknown'}): probes run in demo only"
    await tr.ex.trade_markets(sym)
    if sym not in (t.markets or {}):
        return f"{sym} is not listed on the demo account"
    no = await ks.gate(tr, f"probe {name}")
    return no["error"] if no else None


async def _inside(tr, sym: str, name: str) -> str | None:
    """Under the symbol's lock: the state again, the journal, then the exchange."""
    from .entries import EntryDesk
    from .entry_intents import ON_BOOK
    if no := ks.state_refusal(tr.j, f"probe {name}"):
        return no["error"]
    if tr.j.active_trade(sym):
        return f"{sym} has an open journal trade: probes never touch a symbol the desk uses"
    if EntryDesk(tr)._rows(f"{ON_BOOK} AND symbol=?", (sym,)):
        return f"{sym} has resting entries (or sends of unknown outcome) in the journal: probes need a clean symbol"
    if pos := await tr.position(sym):
        return f"{sym} has a position on the exchange ({pos.get('contracts')}): probes need a clean symbol"
    reg = await tr.t.fetch_open_orders(sym)
    alg = await tr.t.fetch_open_orders(sym, params=kit.TRIG)
    if reg or alg:
        return f"{sym} has {len(reg)} open order(s) and {len(alg)} conditional order(s): probes need a clean symbol"
    return None


# ------------------------------------------------------------ cleanup
async def _cancel_ours(p: kit.Probe, algo: bool) -> None:
    what = "conditional orders" if algo else "orders"
    for o in await p.need(f"cleanup: open {what}", p.t.fetch_open_orders, p.sym, params=kit.TRIG if algo else {}):
        if p.ours(o, algo):
            await p.cancel(f"cleanup: cancel {o.get('id')}", str(o["id"]), algo)


async def _flatten(p: kit.Probe) -> None:
    pos = await p.position("cleanup: position")
    if not pos:
        return
    if pos.get("side") != "long":
        r = await p.need("cleanup: close position", p.tr.close, p.sym, f"probe {p.name}: cleanup")
        if not r.get("ok"):
            raise RuntimeError(r.get("error") or r)
        return
    await p.need("cleanup: close a LONG (reduce-only market SELL)", p.t.create_order, p.sym, "market", "sell",
                 float(pos["contracts"]), None, {"reduceOnly": True, "clientOrderId": p.cid()})
    await p.tr.alert("probe_long", f"probe {p.name} found a LONG {pos.get('contracts')} and sold it back: a reduce-only "
                     "order must never flip a position. Check Binance", p.sym, True)


async def _left(p: kit.Probe) -> dict[str, Any]:
    pos = await p.position("cleanup: position after")
    reg = await p.need("cleanup: orders after", p.t.fetch_open_orders, p.sym, params={})
    alg = await p.need("cleanup: conditional orders after", p.t.fetch_open_orders, p.sym, params=kit.TRIG)
    return {"position": float(pos["contracts"]) if pos else 0.0, "orders": [str(o.get("id")) for o in reg],
            "conditional": [str(o.get("id")) for o in alg]}


async def _cleanup(p: kit.Probe) -> dict[str, Any]:
    errors: list[str] = []
    left: dict[str, Any] | None = None
    steps = (("orders", lambda: _cancel_ours(p, False)), ("position", lambda: _flatten(p)),
             ("conditional orders", lambda: _cancel_ours(p, True)), ("flat again", lambda: _flatten(p)))
    try:
        async with asyncio.timeout(CLEANUP_S):
            if p.countdown:
                ok, _ = await p.call("cleanup: countdown off", p.t.fapiPrivatePostCountdownCancelAll,
                                     {"symbol": p.raw, "countdownTime": 0})
                p.countdown = not ok
            for what, step in steps:
                try:
                    await step()
                except Exception as e:  # noqa: BLE001 - every step runs
                    errors.append(f"{what}: {type(e).__name__}: {e}"[:300])
            left = await _left(p)
    except Exception as e:  # noqa: BLE001 (incl. the timeout)
        errors.append(f"re-read: {type(e).__name__}: {e}"[:300])
    clean = bool(left) and not (left["position"] or left["orders"] or left["conditional"]) and not p.countdown
    if not clean:
        await p.tr.alert("probe_cleanup_incomplete", f"probe {p.name} on {p.sym} left {left or 'an unreadable state'}"
                         f"{' (countdownCancelAll still armed)' if p.countdown else ''}; errors: {errors}. Check "
                         "Binance and close or cancel it by hand", p.sym, True)
    return {"clean": clean, "left": left, "errors": errors}


# ------------------------------------------------------------ running
async def _body(p: kit.Probe, case: Case) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
    out: dict[str, Any] = {}
    err = None
    async with _active(p.tr.j, p.sym, case.position):
        try:
            async with asyncio.timeout(case.budget_s):
                out = await case.fn(p)
        except Exception as e:  # noqa: BLE001 (incl. the budget's TimeoutError): recorded, then cleaned up
            err = f"{type(e).__name__}: {e}"[:500]
        finally:
            cleanup = await _cleanup(p)
    return out, err, cleanup


async def run(tr, name: str, symbol: str = DEFAULT_SYMBOL) -> dict[str, Any]:
    """One probe; its result is journalled (probe_result) and returned."""
    case, sym = CASES.get(name), to_symbol(symbol)
    head = {"probe": name, "number": case.number if case else None, "symbol": sym}
    if case is None:
        return {"ok": False, "refused": True, **head, "error": f"unknown probe {name!r}; one of {', '.join(ORDER)}"}
    started, p = now_iso(), None
    try:
        if why := await _outside(tr, sym, name):
            return {"ok": False, "refused": True, **head, "error": why}
        p = kit.Probe(tr, sym, name, case.number)
        async with tr.symbol_lock(sym, f"probe {name}"):
            if why := await _inside(tr, sym, name):
                return {"ok": False, "refused": True, **head, "error": why}
            out, err, cleanup = await _body(p, case)
    except SymbolBusy as e:
        return {"ok": False, "refused": True, **head, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - a check that couldn't run: nothing was sent
        if p is None or not p.calls:
            return {"ok": False, "refused": True, **head, "error": f"pre-checks failed: {type(e).__name__}: {e}"}
        raise
    res = {"ok": err is None, **head, "mode": tr.ex.settings.mode, "started": started, "finished": now_iso(),
           "verdict": out.get("verdict") or f"error: {err}", "matches_fake": out.get("matches_fake"),
           "fake_assumption": case.fake, "observed": out.get("observed"), **({"error": err} if err else {}),
           "clean": cleanup["clean"], "cleanup": cleanup, "calls": p.calls}
    res["decision_id"] = tr.j.log_decision("probe_result", f"#{case.number} {name}: {res['verdict']}", sym,
                                           snapshot=res)
    return res


async def run_all(tr, symbol: str = DEFAULT_SYMBOL) -> dict[str, Any]:
    """Every probe in ORDER on one symbol; stops at a refusal or a probe that didn't clean up."""
    results: list[dict[str, Any]] = []
    for i, name in enumerate(ORDER):
        if i:
            await kit._sleep(GAP_S)
        r = await run(tr, name, symbol)
        results.append(r)
        if r.get("refused") or not r.get("clean"):
            break
    return {"ok": len(results) == len(ORDER) and all(r.get("clean") for r in results), "results": results}

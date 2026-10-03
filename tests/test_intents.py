"""Intents (#36 gateway step 2, GATEWAY.md §4-5): every write is journalled on receipt, before anything runs, under the
id its caller chose; it runs once per id, and its result, orders (by #9 client id) and decision rows link back to it."""

import asyncio
import dataclasses
import os
import sqlite3
import time

import ccxt.async_support as ccxt
import pytest

from shortbot import audit, clientid, gateway, intents
from shortbot.commands import CommandError
from shortbot.gateway import LocalGateway
from shortbot.intents import Intent
from shortbot.journal import Journal
from shortbot.symlock import SymbolBusy

SYM = "QNT/USDT:USDT"


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    monkeypatch.setattr(audit, "_proc", {"kind": None, "claude_session": None})


@pytest.fixture
def j(tmp_path):
    return Journal(tmp_path / "j.db")


def gw(monkeypatch, j, **handlers):
    for kind, fn in handlers.items():
        monkeypatch.setitem(gateway.HANDLERS, kind, fn)
    return LocalGateway(lambda: j)


def count(j):
    return j.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0]


# ------------------------------------------------------------ journal first, result on the row
async def test_an_intent_is_journalled_before_its_handler_runs_and_keeps_its_result(j, monkeypatch):
    seen = []

    async def act(ctx, symbol, qty=1.0):
        seen.append(intents.get(j, audit.intent_id()))
        return {"ok": True, "qty": qty, "trade_id": 7}
    g = gw(monkeypatch, j, t_act=act)
    with audit.acting("mcp:t_act"):
        it = Intent("t_act", {"symbol": SYM, "qty": 2.0}, SYM)
    assert await g.submit(it) == {"ok": True, "qty": 2.0, "trade_id": 7}
    (running,) = seen
    assert running["status"] == "running" and running["started_at"] and running["finished_at"] is None
    row = intents.get(j, it.id)
    assert (row["status"], row["kind"], row["symbol"], row["side"], row["actor"], row["mode"], row["pid"]) == (
        "done", "t_act", SYM, "short", "mcp:t_act", "demo", os.getpid())
    assert row["args"] == {"symbol": SYM, "qty": 2.0} and row["result"] == {"ok": True, "qty": 2.0, "trade_id": 7}
    assert row["trade_id"] == 7 and row["code_version"] == audit.CODE_VERSION and row["finished_at"]
    assert len(it.id) == 26 and it.id != intents.new_id()   # a ULID: sortable, unique


# ------------------------------------------------------------ idempotency (§4.3)
async def test_a_resubmitted_id_never_runs_twice(j, tmp_path, monkeypatch):
    runs = []

    async def act(ctx, symbol):
        runs.append(symbol)
        return {"ok": True, "n": len(runs)}
    g = gw(monkeypatch, j, t_act=act)
    it = Intent("t_act", {"symbol": SYM}, SYM)
    first = await g.submit(it)
    assert await g.submit(it) == first == {"ok": True, "n": 1}
    other = Journal(tmp_path / "j.db")   # another process on the same journal gets the same answer
    assert await LocalGateway(lambda: other).submit(it) == first and runs == [SYM] and count(j) == 1


async def test_a_resubmitted_failure_answers_its_stored_error(j, monkeypatch):
    runs = []

    async def boom(ctx):
        runs.append(1)
        raise RuntimeError("exchange down")
    g = gw(monkeypatch, j, t_boom=boom)
    it = Intent("t_boom", {})
    with pytest.raises(RuntimeError, match="exchange down"):
        await g.submit(it)
    assert await g.submit(it) == {"ok": False, "intent_id": it.id, "status": "failed",
                                  "error": "RuntimeError: exchange down"}
    assert runs == [1]


async def test_a_resubmission_while_it_runs_answers_in_progress(j, monkeypatch):
    started, gate = asyncio.Event(), asyncio.Event()

    async def slow(ctx):
        started.set()
        await gate.wait()
        return {"ok": True}
    g = gw(monkeypatch, j, t_slow=slow)
    it = Intent("t_slow", {})
    task = asyncio.create_task(g.submit(it))
    await started.wait()
    again = await g.submit(it)
    assert again["ok"] is False and again["unknown"] is True and again["in_progress"] is True
    assert again["status"] == "running" and again["intent_id"] == it.id and "never resend" in again["error"]
    gate.set()
    assert await task == {"ok": True} and intents.get(j, it.id)["status"] == "done"


async def test_a_busy_intent_sent_nothing_and_runs_again_under_its_id(j, monkeypatch):
    calls = []

    async def busy_once(ctx):
        calls.append(1)
        if len(calls) == 1:
            raise SymbolBusy(SYM, 5, "held by pid 1")
        return {"ok": True}
    g = gw(monkeypatch, j, t_busy=busy_once)
    it = Intent("t_busy", {}, SYM)
    with pytest.raises(SymbolBusy):
        await g.submit(it)
    assert intents.get(j, it.id)["status"] == "busy"
    assert await g.submit(it) == {"ok": True} and len(calls) == 2 and intents.get(j, it.id)["status"] == "done"


async def test_an_id_reused_for_another_request_is_refused(j, monkeypatch):
    async def act(ctx, symbol):
        return {"ok": True}
    g = gw(monkeypatch, j, t_act=act)
    it = Intent("t_act", {"symbol": SYM}, SYM)
    await g.submit(it)
    with pytest.raises(ValueError, match="reused"):
        await g.submit(dataclasses.replace(it, args={"symbol": "ABC/USDT:USDT"}))


# ------------------------------------------------------------ what is refused before anything runs
async def test_unknown_kinds_and_bad_args_are_never_journalled(j, monkeypatch):
    async def act(ctx, symbol):
        return {"ok": True}
    g = gw(monkeypatch, j, t_act=act)
    with pytest.raises(ValueError, match="no handler"):
        await g.submit(Intent("t_nope", {}))
    with pytest.raises(TypeError):
        await g.submit(Intent("t_act", {"symbol": SYM, "bogus": 1}))
    assert count(j) == 0


async def test_a_long_is_refused_until_phase_2(j, monkeypatch):
    runs = []

    async def act(ctx, symbol):
        runs.append(1)
        return {"ok": True}
    g = gw(monkeypatch, j, t_act=act)
    it = Intent("t_act", {"symbol": SYM}, SYM, side="long")
    res = await g.submit(it)
    assert res["ok"] is False and "long" in res["error"] and runs == []
    assert intents.get(j, it.id)["status"] == "refused"


async def test_a_callers_refusal_is_journalled_without_running(j, monkeypatch):
    runs = []

    async def act(ctx, symbol):
        runs.append(1)
        return {"ok": True}
    g = gw(monkeypatch, j, t_act=act)
    it = Intent("t_act", {"symbol": SYM}, SYM)
    refusal = {"ok": False, "stale_code": True, "error": "stale code: nothing was sent"}
    assert await g.submit(it, refusal) == refusal and runs == []
    row = intents.get(j, it.id)
    assert row["status"] == "refused" and row["result"] == refusal and row["started_at"] is None


async def test_a_nested_submit_is_a_bug(j, monkeypatch):
    async def inner(ctx):
        return {"ok": True}

    async def outer(ctx):
        return await LocalGateway(lambda: j).submit(Intent("t_inner", {}))
    g = gw(monkeypatch, j, t_inner=inner, t_outer=outer)
    it = Intent("t_outer", {})
    with pytest.raises(RuntimeError, match="nested"):
        await g.submit(it)
    assert intents.get(j, it.id)["status"] == "failed" and count(j) == 1


# ------------------------------------------------------------ status: today's outcomes under §4.5's names
@pytest.mark.parametrize("outcome, status", [
    ({"ok": True}, "done"),
    ({"ok": False, "noop": True, "error": "already there"}, "refused"),
    ({"ok": False, "vetoed": ["hostile"]}, "refused"),
    ({"ok": False, "unknown": True}, "unknown"),
    ({"ok": True, "unknown": [{"entry_id": 3}]}, "unknown"),
    ({"ok": False, "busy": True, "error": "another session is adding risk"}, "busy"),
    (CommandError("no open journal trade"), "refused"),
    (SymbolBusy(SYM, 5, "held"), "busy"),
    (clientid.Unknown("sb-cQNT-1", TimeoutError()), "unknown"),
    (ccxt.RequestTimeout("binanceusdm timed out"), "unknown"),
    (ccxt.InsufficientFunds('binanceusdm {"code":-2019}'), "failed"),
    (RuntimeError("bug"), "failed"),
    (asyncio.CancelledError(), "interrupted"),
])
async def test_status_of_each_outcome(j, monkeypatch, outcome, status):
    async def act(ctx):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome
    g = gw(monkeypatch, j, t_act=act)
    it = Intent("t_act", {})
    if isinstance(outcome, BaseException):
        with pytest.raises(type(outcome)):
            await g.submit(it)
    else:
        assert await g.submit(it) == outcome
    row = intents.get(j, it.id)
    assert row["status"] == status and (row["error"] is not None) == isinstance(outcome, BaseException)


# ------------------------------------------------------------ links: orders by client id, decisions by intent id
async def test_orders_and_rows_written_inside_an_intent_link_to_it(j, monkeypatch):
    cid = clientid.new("close", clientid.tag(SYM))

    async def act(ctx):
        intents.link(j, cid, SYM)
        j.log_decision("close", "plan target", SYM)
        j.log_alert("stop_near", "near", SYM)
        return {"ok": True}
    g = gw(monkeypatch, j, t_act=act)
    it = Intent("t_act", {})
    await g.submit(it)
    intents.link(j, clientid.new("stop", clientid.tag(SYM)), SYM)   # outside any intent: nothing to link
    j.log_decision("note", "outside", SYM)
    assert [(o["client_id"], o["kind"], o["symbol"]) for o in intents.orders(j, it.id)] == [(cid, "close", SYM)]
    assert j.db.execute("SELECT COUNT(*) FROM intent_orders").fetchone()[0] == 1
    assert [(d["action"], d["intent_id"]) for d in j.decisions()] == [("note", None), ("close", it.id)]
    assert j.alerts()[0]["intent_id"] == it.id


def test_intent_rows_are_append_only(j):
    it = Intent("t_act", {"symbol": SYM}, SYM)
    assert intents.receive(j, it) is None and intents.start(j, it.id)
    intents.finish(j, it.id, "done", {"ok": True})
    for sql in ("DELETE FROM intents", "UPDATE intents SET args='{}'", "UPDATE intents SET status='failed'",
                "UPDATE intents SET actor='x'"):
        with pytest.raises(sqlite3.DatabaseError, match="intent"):
            j.db.execute(sql)
    intents.link(j, "sb-cQNT-1", SYM)   # not inside an intent: ignored
    assert intents.get(j, it.id)["status"] == "done"


# ------------------------------------------------------------ a process that died mid-intent (§5, in-process: D2)
def test_the_sweep_ends_the_intents_of_a_dead_process(j, tmp_path):
    ran, waiting = Intent("t_act", {}, SYM), Intent("t_act", {}, SYM)
    intents.receive(j, ran)
    intents.start(j, ran.id)
    with audit.within(ran.id):
        intents.link(j, "sb-oQNT-1", SYM)
    intents.receive(j, waiting)
    assert intents.sweep(j) == []                                    # this process is alive
    live = Journal(tmp_path / "j.db", mode="live")
    assert intents.sweep(live, alive=lambda pid: False) == []        # another mode's rows are its guardian's
    swept = {r["id"]: r for r in intents.sweep(j, alive=lambda pid: False)}
    assert swept[ran.id]["status"] == "interrupted" and swept[ran.id]["orders"] == ["sb-oQNT-1"]
    assert swept[waiting.id]["status"] == "expired" and swept[waiting.id]["orders"] == []
    assert intents.get(j, waiting.id)["status"] == "expired" and intents.get(j, ran.id)["status"] == "interrupted"
    intents.finish(j, ran.id, "done", {"ok": True})                  # its owner was only suspended: the truth wins
    assert intents.get(j, ran.id)["status"] == "done"


def test_the_sweep_ends_an_intent_open_for_too_long_whatever_its_pid(j):
    it = Intent("t_act", {}, SYM)
    intents.receive(j, it)
    intents.start(j, it.id)
    assert intents.sweep(j, now=time.time() + intents.MAX_OPEN_S - 60) == []
    (row,) = intents.sweep(j, now=time.time() + intents.MAX_OPEN_S + 60)
    assert row["id"] == it.id and row["status"] == "interrupted"

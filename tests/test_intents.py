"""Intents (#36 gateway step 2, GATEWAY.md §4-5): every write is journalled on receipt, before anything runs, under the
id its caller chose; it runs once per id, and its result, orders (by #9 client id) and decision rows link back to it."""

import asyncio
import dataclasses
import os
import sqlite3
import time

import ccxt.async_support as ccxt
import pytest

from shortbot import audit, clientid, gateway, intents, procs
from shortbot.commands import CommandError
from shortbot.gateway import LocalGateway
from shortbot.intents import Intent
from shortbot.journal import Journal
from shortbot.results import Result
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
    assert await g.submit(it) == {"ok": False, "intent_id": it.id, "status": "refused",   # it sent nothing
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
    assert intents.get(j, it.id)["status"] == "refused" and count(j) == 1


# ------------------------------------------------------------ status: exact by what the run sent (results.py)
@pytest.mark.parametrize("outcome, sends, status", [
    ({"ok": True}, (), "done"),
    ({"ok": False, "noop": True, "error": "already there"}, (), "refused"),
    ({"ok": False, "vetoed": ["hostile"]}, (), "refused"),
    ({"ok": False, "error": "entry order refused"}, ("rejected",), "failed"),
    ({"ok": False, "unknown": True}, (None,), "unknown"),
    ({"ok": True, "unknown": [{"entry_id": 3}]}, (None,), "unknown"),
    ({"ok": True}, ("part_filled",), "partial"),
    ({"ok": False, "busy": True, "error": "another session is adding risk"}, (), "busy"),
    (CommandError("no open journal trade"), (), "refused"),
    (SymbolBusy(SYM, 5, "held"), (), "busy"),
    (clientid.Unknown("sb-cQNT-1", TimeoutError()), (None,), "unknown"),
    (ccxt.RequestTimeout("binanceusdm timed out"), (), "refused"),    # a read: nothing was sent
    (ccxt.InsufficientFunds('binanceusdm {"code":-2019}'), ("rejected",), "failed"),
    (RuntimeError("bug"), (), "refused"),
    (RuntimeError("bug"), ("filled",), "partial"),
    (asyncio.CancelledError(), (), "interrupted"),
])
async def test_status_of_each_outcome(j, monkeypatch, outcome, sends, status):
    async def act(ctx):
        for i, got in enumerate(sends):   # what a Trader primitive records around each send
            intents.link(j, f"sb-cQNT-{i}", SYM)
            if got is not None:
                intents.settle(j, f"sb-cQNT-{i}", got)
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
    assert [o["outcome"] for o in intents.orders(j, it.id)] == list(sends)


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
    intents.finish(j, it.id, Result("done", False, {"ok": True}))
    for sql in ("DELETE FROM intents", "UPDATE intents SET args='{}'", "UPDATE intents SET status='failed'",
                "UPDATE intents SET actor='x'"):
        with pytest.raises(sqlite3.DatabaseError, match="intent"):
            j.db.execute(sql)
    intents.link(j, "sb-cQNT-1", SYM)   # not inside an intent: ignored
    assert intents.get(j, it.id)["status"] == "done"


def test_an_unknown_intent_is_resolved_once_and_its_orders_outcome_set_once(j):
    it = Intent("t_act", {"symbol": SYM}, SYM)
    intents.receive(j, it)
    intents.start(j, it.id)
    with audit.within(it.id):
        intents.link(j, "sb-cQNT-1", SYM)
    intents.finish(j, it.id, Result("unknown", None, {"ok": False, "unknown": True}))
    for sql in ("UPDATE intents SET result='{}'", "UPDATE intents SET status='failed', resolved_at='x'",
                "UPDATE intents SET status='done'"):   # only resolved: done | refused | partial, stamped
        with pytest.raises(sqlite3.DatabaseError, match="intent"):
            j.db.execute(sql)
    assert intents.record(j, "sb-cQNT-1", "filled") and not intents.record(j, "sb-cQNT-1", "not_found")
    with pytest.raises(sqlite3.DatabaseError, match="outcome"):
        j.db.execute("UPDATE intent_orders SET outcome='not_found'")
    assert intents.resolve(j, it.id, "unknown", "done", intents.now_ms())
    assert not intents.resolve(j, it.id, "unknown", "refused", intents.now_ms())
    row = intents.get(j, it.id)
    assert (row["status"], row["result"], bool(row["resolved_at"])) == ("done", {"ok": False, "unknown": True}, True)
    with pytest.raises(sqlite3.DatabaseError, match="intent"):
        j.db.execute("UPDATE intents SET status='partial'")


def test_unsettled_lists_a_finished_intents_sends_still_in_doubt_and_nothing_else(j):
    """#86: an intent that finished (not unknown or interrupted: the resolver's) with a send of no outcome."""
    rows = {}
    for st in ("done", "unknown", "running"):
        it = Intent("t_act", {"symbol": SYM}, SYM)
        intents.receive(j, it)
        intents.start(j, it.id)
        with audit.within(it.id):
            intents.link(j, f"sb-c{st}-1", SYM)
            intents.link(j, f"sb-c{st}-2", SYM)
        intents.record(j, f"sb-c{st}-2", "filled")
        if st != "running":
            intents.finish(j, it.id, Result(st, True, {"ok": True}))
        rows[st] = it.id
    assert [(r["intent_id"], r["client_id"]) for r in intents.unsettled(j)] == [(rows["done"], "sb-cdone-1")]


def test_an_old_journal_gets_the_new_columns_and_triggers(tmp_path):
    db = sqlite3.connect(tmp_path / "old.db")
    db.executescript("""
        CREATE TABLE intents (id TEXT PRIMARY KEY, kind TEXT NOT NULL, symbol TEXT, side TEXT NOT NULL,
            args TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT, trade_id INTEGER, actor TEXT NOT NULL,
            claude_session TEXT, session_id INTEGER, code_version TEXT, mode TEXT NOT NULL, pid INTEGER NOT NULL,
            received_at TEXT NOT NULL, started_at TEXT, finished_at TEXT);
        CREATE TABLE intent_orders (client_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL, kind TEXT NOT NULL,
            symbol TEXT NOT NULL, at TEXT NOT NULL);
        CREATE TRIGGER intents_final BEFORE UPDATE ON intents WHEN OLD.status IN ('done', 'unknown')
            BEGIN SELECT RAISE(ABORT, 'a finished intent never changes'); END;
        CREATE TRIGGER intent_orders_frozen BEFORE UPDATE ON intent_orders
            BEGIN SELECT RAISE(ABORT, 'an intent''s order link never changes'); END;""")
    db.close()
    j = Journal(tmp_path / "old.db")
    names = {r[0] for r in j.db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"intents_settled", "intent_orders_settled"} <= names and not names & set(intents.RETIRED)
    assert {"sent", "resolved_at", "journal_late", "ns"} <= {r[1] for r in j.db.execute("PRAGMA table_info(intents)")}
    assert "intents_ns_frozen" in names
    assert "outcome" in {r[1] for r in j.db.execute("PRAGMA table_info(intent_orders)")}
    Journal(tmp_path / "old.db")   # a second open changes nothing


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
    intents.finish(j, ran.id, Result("done", True, {"ok": True}))     # its owner was only suspended: the truth wins
    assert intents.get(j, ran.id)["status"] == "done"


def test_the_sweep_ends_an_intent_open_for_too_long_whatever_its_pid(j):
    it = Intent("t_act", {}, SYM)
    intents.receive(j, it)
    intents.start(j, it.id)
    assert intents.sweep(j, now=time.time() + intents.MAX_OPEN_S - 60) == []
    (row,) = intents.sweep(j, now=time.time() + intents.MAX_OPEN_S + 60)
    assert row["id"] == it.id and row["status"] == "interrupted"


def test_the_sweep_judges_a_pid_only_in_its_own_pid_namespace(j, monkeypatch):
    """EC2 (2026-10-04): the guardian's container can't see the host's pids, so os.kill calls a live MCP server dead;
    the web container runs as pid 1, which in the guardian's namespace is the guardian itself, so a dead web intent
    looked alive forever. A row from another pid namespace (or from before the column) is never judged by its pid:
    only MAX_OPEN_S ends it."""
    mine = Intent("t_act", {}, SYM)
    intents.receive(j, mine)
    monkeypatch.setattr(intents, "NS", "pid:[4026532999]")   # received in the web container, as its pid 1
    monkeypatch.setattr(intents.os, "getpid", lambda: 1)
    web = Intent("t_act", {}, SYM)
    intents.receive(j, web)
    intents.start(j, web.id)
    monkeypatch.setattr(intents, "NS", None)                  # a row from before the column: namespace unknown
    legacy = Intent("t_act", {}, SYM)
    intents.receive(j, legacy)
    monkeypatch.undo()
    assert [(intents.get(j, i.id)["ns"], intents.get(j, i.id)["pid"]) for i in (mine, web, legacy)] == [
        (procs.NS, os.getpid()), ("pid:[4026532999]", 1), (None, 1)]
    assert intents.sweep(j) == []                             # this process is alive; pid 1 is never asked
    (swept,) = intents.sweep(j, alive=lambda pid: False)      # only the row whose pid this process can judge
    assert swept["id"] == mine.id and swept["status"] == "expired"
    late = {r["id"]: r["status"] for r in intents.sweep(j, now=time.time() + intents.MAX_OPEN_S + 60)}
    assert late == {web.id: "interrupted", legacy.id: "expired"}   # pid 1 is alive here: the age rule ends them
    with pytest.raises(sqlite3.DatabaseError, match="request"):
        j.db.execute("UPDATE intents SET ns='x'")

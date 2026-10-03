"""Audit trail (#24), order paths and the read side: a refusal or failure on an order path leaves a `rejected` /
`failed` row unless the path journalled its own decision; repeats are deduped; `shortbot audit` lists who did what;
`shortbot hook-session` records the Claude session's model."""

import io
import json
import sys

import pytest

from shortbot import audit, audit_view, cli, intents
from shortbot.intents import Intent
from shortbot.journal import Journal
from tests.fakes import SYM
from tests.test_trading import open_default, trader


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    monkeypatch.setattr(audit, "_proc", {"kind": None, "claude_session": None})
    monkeypatch.setattr(audit, "_seen", {})


class Desk:
    def __init__(self, j):
        self.j, self.calls = j, 0

    @audit.refusals("inner")
    async def inner(self, symbol: str) -> dict:
        self.calls += 1
        return {"ok": False, "error": f"new stop must be above mark {1.2 + self.calls / 100:.2f}"}

    @audit.refusals("outer")
    async def outer(self, symbol: str, notional: float = 10) -> dict:
        r = await self.inner(symbol)
        return {"ok": False, "error": f"could not tighten first: {r['error']}", "trade_id": 4}

    @audit.refusals("logs_its_own")
    async def logs_its_own(self, symbol: str) -> dict:
        self.j.log_decision("rejected", "vetoes: hostile regime", symbol)
        return {"ok": False, "vetoed": ["hostile regime"]}

    @audit.refusals("noop")
    async def noop(self, symbol: str) -> dict:
        return {"ok": False, "noop": True, "error": "already there"}

    @audit.refusals("fine")
    async def fine(self, symbol: str) -> dict:
        return {"ok": True}

    @audit.refusals("boom")
    async def boom(self, symbol: str) -> dict:
        raise RuntimeError("timeout?signature=abc")


async def test_refusals_are_logged_once_per_path(tmp_path):
    j = Journal(tmp_path / "j.db")
    d = Desk(j)
    await d.outer("QNT/USDT:USDT", notional=25)
    await d.logs_its_own("ABC/USDT:USDT")
    await d.noop("ABC/USDT:USDT")
    await d.fine("ABC/USDT:USDT")
    with pytest.raises(RuntimeError):
        await d.boom("ABC/USDT:USDT")
    rows = j.decisions()[::-1]
    assert [(r["action"], r["reasoning"].split(":")[0]) for r in rows] == [
        ("rejected", "inner"), ("rejected", "outer"), ("rejected", "vetoes"), ("failed", "boom")]
    inner, outer, _, boom = rows
    assert inner["symbol"] == outer["symbol"] == "QNT/USDT:USDT" and outer["trade_id"] == 4
    snap = json.loads(outer["snapshot"])
    assert snap["op"] == "outer" and snap["args"] == {"symbol": "QNT/USDT:USDT", "notional": 25}
    assert snap["result"]["error"].startswith("could not tighten first")
    assert "abc" not in boom["reasoning"] and json.loads(boom["snapshot"])["error"].startswith("RuntimeError")


async def test_an_identical_refusal_is_written_once_per_window_then_with_its_count(tmp_path, monkeypatch):
    j = Journal(tmp_path / "j.db")
    d, now = Desk(j), [1000.0]
    monkeypatch.setattr(audit, "_now", lambda: now[0])
    with audit.acting("guardian:positions"):
        for _ in range(3):
            await d.inner("QNT/USDT:USDT")   # the mark moves, the refusal is the same
            now[0] += 60
        await d.inner("ABC/USDT:USDT")       # another symbol: its own row
        now[0] += audit.DEDUPE_S
        await d.inner("QNT/USDT:USDT")
    rows = j.decisions()[::-1]
    assert [r["symbol"] for r in rows] == ["QNT/USDT:USDT", "ABC/USDT:USDT", "QNT/USDT:USDT"]
    assert "suppressed" not in json.loads(rows[0]["snapshot"])
    assert json.loads(rows[2]["snapshot"])["suppressed"] == 2
    with audit.acting("mcp:move_stop"):   # another actor is never folded into the guardian's count
        await d.inner("QNT/USDT:USDT")
    assert len(j.decisions()) == 4


async def test_open_short_refusals_reach_the_journal_once():
    t, fake = trader()
    r = await t.open_short("OTHER/USDT:USDT", stop_price=1.05, setup="pump_fade", thesis="t", notional=100)
    assert not r["ok"] and fake.orders == []
    r = await open_default(t, plan={"targets": [{"price": 1.2, "fraction": 1}]})
    assert r["plan_errors"]
    r = await open_default(t, stop_price=0.95)   # the risk check journals its own rejection: no second row
    assert r["rejected_by_risk"]
    rows = t.j.decisions()[::-1]
    assert [x["action"] for x in rows] == ["rejected"] * 3 and not rows[2]["reasoning"].startswith("open_short")
    assert rows[0]["reasoning"].startswith("open_short: ") and "not listed" in rows[0]["reasoning"]
    assert rows[1]["reasoning"].startswith("open_short: ") and "below entry" in rows[1]["reasoning"]


async def test_a_noop_move_stop_writes_nothing():
    t, fake = trader()
    await open_default(t)
    n = len(t.j.decisions())
    assert (await t.move_stop(SYM, 1.0500001, "trail"))["noop"]
    assert len(t.j.decisions()) == n


def test_audit_view_filters_and_names_the_model(tmp_path, monkeypatch):
    j = Journal(tmp_path / "j.db")
    audit.set_process("hook", claude_session="S1")
    j.log_decision("claude_session", "startup: claude-opus-5", snapshot={"model": "claude-opus-5"})
    audit.set_process("mcp", claude_session="S1")
    with audit.acting("mcp:open_short"):   # a write refused before it ran (#36: its intent row is its record)
        intents.receive(j, Intent("open_short", {"symbol": "QNT"}, "QNT/USDT:USDT"), {"ok": False, "vetoed": ["x"]})
    audit.set_process("guardian", claude=False)
    with audit.acting("guardian:positions"):
        j.log_decision("move_stop", "plan trail", "QNT/USDT:USDT")
    j.db.execute("INSERT INTO decisions (ts, action, reasoning) VALUES ('2020-01-01T00:00:00+00:00', 'hold', 'old')")
    out = audit_view.report(j, since="24h")
    assert [r["actor"] for r in out["rows"] if r["action"] != "intent"] == ["guardian:positions", "hook"]
    assert {r["actor"] for r in out["rows"]} == {"guardian:positions", "mcp:open_short", "hook"}   # one second apart
    assert out["by_actor"] == {"guardian:positions": 1, "mcp:open_short": 1, "hook": 1}
    assert out["claude_sessions"]["S1"]["model"] == "claude-opus-5" and out["unstamped_rows"] == 0
    (call,) = [r for r in out["rows"] if r["action"] == "intent"]
    assert (call["action"], call["kind"], call["status"], call["args"]) == ("intent", "open_short", "refused",
                                                                           {"symbol": "QNT"})
    assert [r["kind"] for r in audit_view.report(j, action="intent")["rows"]] == ["open_short"]
    assert [r["actor"] for r in audit_view.report(j, actor="mcp")["rows"]] == ["mcp:open_short"]
    assert audit_view.report(j)["unstamped_rows"] == 1
    assert [r["action"] for r in audit_view.report(j, action="hold")["rows"]] == ["hold"]
    assert j.decisions()[0]["action"] == "hold" and {d["action"] for d in j.decisions()} == {"hold", "move_stop"}


def test_hook_session_records_the_model_quietly(tmp_path, monkeypatch, capsys):
    db = tmp_path / "data" / "shortbot.db"
    monkeypatch.setenv("SHORTBOT_DB", str(db))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S-env")
    monkeypatch.setattr(cli, "get_app", lambda *a, **k: pytest.fail("the hook must not build the exchange app"))
    payload = {"session_id": "S-hook", "source": "startup", "model": "claude-opus-5", "hook_event_name": "SessionStart",
               "transcript_path": "/x.jsonl", "permission_mode": "default"}
    for stdin in (json.dumps(payload), "not json"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        monkeypatch.setattr(sys, "argv", ["shortbot", "hook-session"])
        with pytest.raises(SystemExit) as e:
            cli.main()
        assert e.value.code == 0 and capsys.readouterr().out == ""   # SessionStart stdout would enter the context
    (row,) = Journal(db).decisions(calls=True)
    assert (row["action"], row["actor"], row["claude_session"]) == ("claude_session", "hook:SessionStart", "S-hook")
    assert json.loads(row["snapshot"]) == {"model": "claude-opus-5", "source": "startup", "permission_mode": "default"}

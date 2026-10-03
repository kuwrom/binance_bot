"""Audit trail (#24): every decision and alert row says who acted (MCP tool, CLI command, guardian job), from which
Claude Code session, on which code and in which mode; mutating tool / CLI calls leave a `call` row, failed ones too;
decisions are append-only."""

import argparse
import json
import sqlite3
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from shortbot import audit, cli, mcp_research, mcp_trade, procs
from shortbot.guardian_loop import Job, Lane
from shortbot.journal import Journal
from shortbot.mcp_server import TOOLS

OLD_SCHEMA = """
CREATE TABLE sessions (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, mode TEXT NOT NULL, notes TEXT);
CREATE TABLE decisions (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, session_id INTEGER, symbol TEXT,
    action TEXT NOT NULL, reasoning TEXT NOT NULL, snapshot TEXT);
CREATE TABLE alerts (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, symbol TEXT, kind TEXT NOT NULL, message TEXT NOT NULL);
INSERT INTO decisions (ts, action, reasoning) VALUES ('2026-09-30T00:00:00+00:00', 'hold', 'before the audit trail');
"""


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    """Each test is its own process as far as the audit stamp goes."""
    monkeypatch.setattr(audit, "_proc", {"kind": None, "claude_session": None})
    monkeypatch.setattr(audit, "_seen", {})


def _cols(j: Journal, table: str) -> set[str]:
    return {r[1] for r in j.db.execute(f"PRAGMA table_info({table})")}


def _rows(j: Journal, table: str) -> list[dict]:
    return j._rows(f"SELECT * FROM {table} ORDER BY id")


def test_an_old_journal_gains_the_audit_columns_and_keeps_its_rows(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(OLD_SCHEMA)
    con.close()
    a, b = Journal(db), Journal(db)   # the guardian and an MCP server opening it at once
    assert {"actor", "claude_session", "code_version", "trade_id", "mode", "session_id"} <= _cols(a, "decisions")
    assert {"actor", "claude_session", "code_version", "mode", "session_id"} <= _cols(b, "alerts")
    assert {"actor", "claude_session", "code_version"} <= _cols(b, "sessions")
    old = _rows(a, "decisions")[0]
    assert old["reasoning"] == "before the audit trail" and old["actor"] is None   # unstamped: an old process wrote it


def test_decisions_are_append_only(tmp_path):
    j = Journal(tmp_path / "j.db")
    did = j.log_decision("hold", "thesis intact")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        j.db.execute("UPDATE decisions SET reasoning='rewritten' WHERE id=?", (did,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        j.db.execute("DELETE FROM decisions WHERE id=?", (did,))
    j.db.rollback()
    assert [d["reasoning"] for d in j.decisions()] == ["thesis intact"]


def test_the_stamp_trade_id_and_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    audit.set_process("cli")
    j = Journal(tmp_path / "j.db", mode="live")
    with audit.acting("cli:close"):
        j.log_decision("partial_close", "T1", "QNT/USDT:USDT", snapshot={"trade_id": 7, "fraction": 0.5})
        j.log_alert("stop_near", "QNT near stop", "QNT/USDT:USDT")
    j.log_decision("note", "outside any call")
    d1, d2 = _rows(j, "decisions")
    assert (d1["actor"], d1["claude_session"], d1["trade_id"], d1["mode"]) == ("cli:close", "abc-123", 7, "live")
    assert d1["code_version"] == procs.code_version() == audit.CODE_VERSION and len(d1["code_version"]) == 64
    assert d2["actor"] == "cli" and d2["trade_id"] is None
    (al,) = _rows(j, "alerts")
    assert (al["actor"], al["claude_session"], al["mode"], al["code_version"]) == (
        "cli:close", "abc-123", "live", audit.CODE_VERSION)


def test_signed_urls_never_reach_a_decision_row(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.log_decision("widen_stop_failed", "GET https://fapi?timestamp=1&signature=deadbeef failed", "X/USDT:USDT",
                   snapshot={"error": "x?signature=deadbeef", "nested": [{"api_secret": "s3cret", "fan_token": 2}]})
    (d,) = _rows(j, "decisions")
    assert "deadbeef" not in d["reasoning"] + d["snapshot"] and "s3cret" not in d["snapshot"]
    snap = json.loads(d["snapshot"])
    assert snap["error"] == "x?signature=<redacted>" and snap["nested"][0]["fan_token"] == 2   # a sector, not a secret


def _app(j, **kw):
    return SimpleNamespace(journal=j, settings=SimpleNamespace(mode=j.mode), ex=SimpleNamespace(experiment=None), **kw)


def _ns(cmd: str, **kw) -> argparse.Namespace:
    return argparse.Namespace(cmd=cmd, **kw)


async def test_mcp_cli_and_guardian_rows_all_carry_actor_code_version_and_mode(tmp_path, monkeypatch):
    db = tmp_path / "j.db"
    # the MCP server of Claude Code session S, which starts a trading session
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "S")
    audit.set_process("mcp")
    jm = Journal(db)
    monkeypatch.setattr(mcp_research, "get_app", lambda: _app(jm))
    sid = (await TOOLS["start_session"]("audit test"))["session_id"]
    await TOOLS["log_decision"]("watch", "lower high forming", "QNT/USDT:USDT")
    await TOOLS["add_learning"]("pumps fade", "trades 1-3", ["trade:1"], "statistical")
    # a CLI command the same Claude session runs through Bash: another process, another Journal
    audit.set_process("cli")
    jc = Journal(db)
    monkeypatch.setattr(cli, "get_app", lambda *a, **k: _app(jc))

    async def no_close():
        return None
    monkeypatch.setattr(cli, "close_app", no_close)
    await cli._audited(_ns("log", action="skip", reasoning="too thin", symbol="ABC"))
    monkeypatch.setattr(mcp_research, "get_app", lambda: _app(jc))   # the CLI runs the same declared bodies
    await cli._audited(_ns("learn", statement="s", evidence="e", setup=None, status="hypothesis",
                           confidence="low", provenance=["trade:2"], kind="statistical", n=None, days=None,
                           sample=None, review_by=None))
    # the guardian, started from that session's Bash, so it inherited CLAUDE_CODE_SESSION_ID
    audit.set_process("guardian", claude=False)
    jg = Journal(db)

    async def positions_job():
        jg.log_decision("trail", "plan trail", "QNT/USDT:USDT", snapshot={"trade_id": 3})
        jg.log_alert("stop_near", "QNT near stop", "QNT/USDT:USDT")
    lane = Lane("protective", [Job("positions", positions_job, 0, 5)], None, None, jg)
    await lane._run(lane.jobs[0])

    rows = _rows(jg, "decisions")
    assert all(r["actor"] and r["code_version"] == audit.CODE_VERSION and r["mode"] == "demo" for r in rows)
    by = {(r["actor"], r["action"]): r for r in rows}
    assert set(by) == {("mcp:start_session", "call"), ("mcp:log_decision", "watch"), ("mcp:add_learning", "call"),
                       ("cli:log", "skip"), ("cli:learn", "call"), ("guardian:positions", "trail")}
    for key in [k for k in by if not k[0].startswith("guardian")]:
        assert (by[key]["claude_session"], by[key]["session_id"]) == ("S", sid), key
    g = by[("guardian:positions", "trail")]
    assert (g["claude_session"], g["session_id"], g["trade_id"]) == (None, None, 3)
    (al,) = _rows(jg, "alerts")
    assert (al["actor"], al["claude_session"], al["mode"]) == ("guardian:positions", None, "demo")
    (s,) = _rows(jg, "sessions")
    assert (s["id"], s["actor"], s["claude_session"], s["code_version"]) == (
        sid, "mcp:start_session", "S", audit.CODE_VERSION)
    call = json.loads(by[("cli:learn", "call")]["snapshot"])
    assert call["outcome"] == "ok" and call["args"]["statement"] == "s" and "cmd" not in call["args"]


async def test_a_failed_tool_call_is_logged(tmp_path, monkeypatch):
    j = Journal(tmp_path / "j.db")
    monkeypatch.setattr(mcp_trade, "get_app", lambda: _app(j))
    with pytest.raises(ToolError, match="no open journal trade"):
        await TOOLS["update_plan"]("QNT", {"time_stop_hours": 30}, "longer hold")

    async def boom(*a):
        raise RuntimeError("exchange down")
    monkeypatch.setattr(mcp_trade, "get_app", lambda: _app(j, trader=SimpleNamespace(move_stop=boom)))
    with pytest.raises(ToolError, match="RuntimeError: exchange down"):
        await TOOLS["move_stop"]("QNT", 1.2, "trail")
    plan_call, stop_call = j.decisions(calls=True)[::-1]
    assert (plan_call["action"], plan_call["actor"], plan_call["symbol"]) == ("call", "mcp:update_plan", "QNT/USDT:USDT")
    assert json.loads(plan_call["snapshot"])["outcome"] == "error" and "no open journal trade" in plan_call["reasoning"]
    snap = json.loads(stop_call["snapshot"])
    assert snap["outcome"] == "error" and snap["error"] == "RuntimeError: exchange down"
    assert snap["args"] == {"symbol": "QNT", "new_stop": 1.2, "reason": "trail"}
    assert j.decisions() == []   # call rows stay out of the recent-decisions view


async def test_refused_and_read_tool_calls(tmp_path, monkeypatch):
    j = Journal(tmp_path / "j.db")

    async def refuse(sym, price, reason):
        return {"ok": False, "error": f"no open position in {sym}"}

    async def positions():
        return []
    tr = SimpleNamespace(move_stop=refuse, positions=positions)
    monkeypatch.setattr(mcp_trade, "get_app", lambda: _app(j, trader=tr))
    await TOOLS["move_stop"]("QNT", 1.2, "see https://x?timestamp=1&signature=abc")
    await TOOLS["positions"]()   # a read: no row
    (row,) = j.decisions(calls=True)
    snap = json.loads(row["snapshot"])
    assert snap["outcome"] == "refused" and "no open position" in row["reasoning"]
    assert snap["args"]["reason"] == "see https://x?timestamp=<redacted>&signature=<redacted>"


async def test_the_writes_are_the_logged_tools():
    from shortbot import commands
    from shortbot.mcp_server import mcp
    names = {t.name for t in await mcp.list_tools()}
    writes = {c.name for c in commands.REGISTRY if c.name and c.write}
    assert writes < names and {"open_short", "close_position", "set_risk_params"} <= writes
    assert not {"positions", "log_decision", "scan_shorts"} & writes   # log_decision's own row is the record
    from shortbot import mcp_book, mcp_desk, mcp_kill
    for mod in (mcp_trade, mcp_research, mcp_desk, mcp_kill, mcp_book):   # the call row finds the journal through it
        assert callable(getattr(mod, "get_app", None)), mod.__name__


def test_the_cli_order_paths_are_logged_writes():
    from shortbot import commands
    reads = {c.cli for c in commands.load() if c.cli and not c.write}
    assert {"positions", "scan", "log", "brief", "audit"} <= reads
    assert not {"open", "close", "move-stop", "propose", "flatten", "kill-switch", "ladder", "probe"} & reads

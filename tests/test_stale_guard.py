"""A stale MCP server refuses state-changing tools (#68): the code it imported (procs.code_version) is compared with the
code on disk (procs.disk_codes, as the #58 audit does) before every tool call. Writes are refused with nothing sent,
reads run with a `stale_code` warning, and a read that crashes on the mismatch says so instead of a bare KeyError."""

import json
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from shortbot import audit, mcp_desk, mcp_kill, mcp_research, mcp_trade, procs, provenance, stale_guard
from shortbot.journal import Journal
from shortbot.mcp_server import mcp

NEW = "f" * 64


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    monkeypatch.setattr(audit, "_proc", {"kind": None, "claude_session": None})
    monkeypatch.setattr(audit, "_seen", {})


def _app(j, **kw):
    return SimpleNamespace(journal=j, settings=SimpleNamespace(mode=j.mode), ex=SimpleNamespace(experiment=None), **kw)


def _disk_is(monkeypatch, code):
    """The code on disk is now `code` (a merge landed after this server started)."""
    hashed = []

    def sha(*a, **k):
        hashed.append(a)
        if isinstance(code, Exception):
            raise code
        return code
    monkeypatch.setattr(provenance, "code_sha", sha)
    return hashed


def _never(*a, **k):
    pytest.fail("a stale server must not reach the trader / exchange")


def _desk(monkeypatch, j, **trader):
    app = _app(j, trader=SimpleNamespace(**trader))
    for mod in (mcp_trade, mcp_research, mcp_desk, mcp_kill):
        monkeypatch.setattr(mod, "get_app", lambda: app)
    return app


def _call_rows(j):
    return [json.loads(r["snapshot"]) | {"reasoning": r["reasoning"], "actor": r["actor"]}
            for r in j.decisions(calls=True)]


# ------------------------------------------------------------ writes
async def test_a_write_is_refused_on_stale_code_and_nothing_is_sent(tmp_path, monkeypatch):
    j = Journal(tmp_path / "j.db")
    _desk(monkeypatch, j, open_short=_never, move_stop=_never)
    _disk_is(monkeypatch, NEW)
    res = await mcp_trade.open_short("QNT", 1.2, "pump_fade", "turned")
    assert res["ok"] is False and res["stale_code"] is True
    old = procs.code_version()[:12]
    assert res["error"] == (f"stale code: this MCP server started on {old}, the code on disk is {NEW[:12]}; ask the "
                            "human to reconnect shortbot via /mcp, or run the same action through the CLI (a fresh "
                            "process on the code on disk): `.venv/bin/shortbot open SYMBOL --stop STOP_PRICE --setup "
                            "SETUP --thesis THESIS [--plan JSON] [--notional USDT] [--leverage N] [--conviction C] "
                            "[--veto-override REASON] [--budget-override REASON]`. Nothing was sent.")
    res = await mcp_trade.move_stop("QNT", 1.1, "trail")
    assert res["stale_code"] is True
    rows = _call_rows(j)
    assert [(r["name"], r["outcome"], r["actor"]) for r in rows] == [
        ("move_stop", "refused", "mcp:move_stop"), ("open_short", "refused", "mcp:open_short")]
    assert all("stale code" in r["reasoning"] for r in rows)


async def test_a_write_runs_when_the_code_on_disk_matches(tmp_path, monkeypatch):
    """The real hash of this checkout (a worktree, when the tests run from one) matches the code imported here."""
    j = Journal(tmp_path / "j.db")
    sent = []

    async def move_stop(*a):
        sent.append(a)
        return {"ok": True, "stop": a[1]}
    _desk(monkeypatch, j, move_stop=move_stop)
    res = await mcp_trade.move_stop("QNT", 1.1, "trail")
    assert res == {"ok": True, "stop": 1.1} and sent == [("QNT", 1.1, "trail")]
    (row,) = _call_rows(j)
    assert row["outcome"] == "ok"


async def test_flatten_all_is_refused_with_the_cli_fallback(tmp_path, monkeypatch):
    j = Journal(tmp_path / "j.db")
    _desk(monkeypatch, j)
    monkeypatch.setattr(mcp_kill.killswitch_ops, "flatten", _never)
    _disk_is(monkeypatch, NEW)
    res = await mcp_kill.flatten_all("squeeze")
    assert res["ok"] is False and res["stale_code"] is True
    assert "shortbot flatten --reason" in res["error"] and "Nothing was sent" in res["error"]


async def test_halt_and_log_decision_still_run_on_stale_code_annotated(tmp_path, monkeypatch):
    """Exempt: a halt only removes risk; log_decision is the agent's reasoning record (append-only, named columns)."""
    j = Journal(tmp_path / "j.db")
    _desk(monkeypatch, j)
    halted = []

    async def halt(tr, state, by, why):
        halted.append((state, why))
        return {"ok": True, "state": state}
    monkeypatch.setattr(mcp_kill.killswitch_ops, "halt", halt)
    _disk_is(monkeypatch, NEW)
    res = await mcp_kill.halt_trading("squeeze")
    assert res["ok"] is True and halted == [("HALTED", "squeeze")] and "stale code" in res["stale_code"]
    res = await mcp_research.log_decision("watch", "lower high forming", "QNT/USDT:USDT")
    assert res["ok"] is True and "stale code" in res["stale_code"]
    assert [d["action"] for d in j.decisions()] == ["watch"]


# ------------------------------------------------------------ reads
async def test_a_dict_read_runs_with_a_warning(tmp_path, monkeypatch):
    j = Journal(tmp_path / "j.db")
    acct = {"equity": 100.0}

    async def account():
        return acct
    _desk(monkeypatch, j, account=account)
    _disk_is(monkeypatch, NEW)
    res = await mcp_trade.account()
    assert res["equity"] == 100.0
    assert res["stale_code"].startswith(f"stale code: this MCP server started on {procs.code_version()[:12]}, the code "
                                        f"on disk is {NEW[:12]}")
    assert "/mcp" in res["stale_code"] and acct == {"equity": 100.0}   # a copy: the tool's own dict is untouched
    assert _call_rows(j) == []   # reads leave no call row


async def test_list_and_str_reads_are_wrapped_and_pass_the_output_schema(tmp_path, monkeypatch):
    j = Journal(tmp_path / "j.db")

    async def positions():
        return [{"symbol": "QNT/USDT:USDT"}]
    _desk(monkeypatch, j, positions=positions)
    monkeypatch.setattr("shortbot.market.market_map.cached", lambda *a: {"m": 1})
    monkeypatch.setattr("shortbot.market.market_map.render", lambda m: "TAPE")
    _disk_is(monkeypatch, NEW)
    for name, want in (("positions", [{"symbol": "QNT/USDT:USDT"}]), ("market_map", "TAPE")):
        res = await mcp.call_tool(name, {})
        assert not res.is_error, res
        assert res.structured_content["result"] == want and "stale code" in res.structured_content["stale_code"]
        (text,) = res.content
        assert json.loads(text.text) == res.structured_content


async def test_reads_are_untouched_when_current(tmp_path, monkeypatch):
    j = Journal(tmp_path / "j.db")

    async def positions():
        return [{"symbol": "QNT/USDT:USDT"}]
    _desk(monkeypatch, j, positions=positions)
    assert await mcp_trade.positions() == [{"symbol": "QNT/USDT:USDT"}]


async def test_a_read_that_crashes_on_stale_code_says_so(tmp_path, monkeypatch):
    """binance-bot-d5: old trading.py in memory, new risk_budget.py lazily imported -> KeyError: 'equity_peak'."""
    j = Journal(tmp_path / "j.db")
    _desk(monkeypatch, j)

    async def gs(trader, verdict):
        raise KeyError("equity_peak")

    async def regime(ex):
        return {"verdict_for_shorts": "neutral"}
    monkeypatch.setattr("shortbot.risk_budget.governor_status", gs)
    monkeypatch.setattr(mcp_desk.scanner, "market_regime", regime)
    _disk_is(monkeypatch, NEW)
    with pytest.raises(ToolError) as e:
        await mcp_desk.governor_status()
    msg = str(e.value)
    assert msg.startswith(f"stale code: this MCP server started on {procs.code_version()[:12]}")
    assert "KeyError: 'equity_peak'" in msg and "governor_status" in msg and "/mcp" in msg


async def test_a_package_dir_that_is_gone_is_stale(tmp_path, monkeypatch):
    """A removed worktree: as procs.audit reports it."""
    j = Journal(tmp_path / "j.db")
    _desk(monkeypatch, j, move_stop=_never)
    monkeypatch.setattr(provenance, "PKG", tmp_path / "gone")
    res = await mcp_trade.move_stop("QNT", 1.1, "trail")
    assert res["stale_code"] is True and f"its code directory {tmp_path / 'gone'} is gone" in res["error"]


# ------------------------------------------------------------ the check itself
async def test_the_disk_hash_is_cached_for_a_few_seconds(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(stale_guard, "_clock", lambda: clock[0])
    hashed = _disk_is(monkeypatch, procs.code_version())
    for _ in range(3):
        assert (await stale_guard.verdict()).ok
    assert len(hashed) == 1
    clock[0] += stale_guard.TTL_S - 0.1
    await stale_guard.verdict()
    assert len(hashed) == 1
    clock[0] += 0.2
    _disk_is(monkeypatch, NEW)   # a merge landed meanwhile
    v = await stale_guard.verdict()
    assert not v.ok and v.stale


async def test_a_failed_hash_refuses_writes_and_lets_reads_run(tmp_path, monkeypatch):
    j = Journal(tmp_path / "j.db")

    async def account():
        return {"equity": 100.0}
    _desk(monkeypatch, j, move_stop=_never, account=account)
    hashed = _disk_is(monkeypatch, PermissionError("denied: shortbot/x.py"))
    res = await mcp_trade.move_stop("QNT", 1.1, "trail")
    assert res["ok"] is False and res["code_check_failed"] is True and "stale_code" not in res
    assert "PermissionError: denied: shortbot/x.py" in res["error"] and "Nothing was sent" in res["error"]
    assert await mcp_trade.account() == {"equity": 100.0}   # fail open for reads, no warning
    (row,) = _call_rows(j)
    assert row["outcome"] == "refused"
    assert len(hashed) == 2   # a failure is not cached: the next call retries

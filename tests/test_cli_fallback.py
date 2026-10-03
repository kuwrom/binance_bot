"""The CLI is the stale MCP server's fallback (#68 follow-up): every state-changing MCP tool has a CLI form, a refusal
names it, and the commands added for it (update-plan, set-params, reset-params, update-learning, start-session) run
the MCP tool's own code, so both paths leave the same journal effect."""

import json
import os
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

from shortbot import audit, cli, mcp_research, mcp_trade, provenance, stale_guard
from shortbot.cli_args import build_parser
from shortbot.config import ResearchBounds, RiskLimits, Settings
from shortbot.journal import Journal
from shortbot import commands
from shortbot.mcp_server import TOOLS
from shortbot.params import ResearchParams

NEW = "f" * 64
VOLATILE = {"id", "ts", "started_at", "created_at", "updated_at", "opened_at", "actor", "claude_session",
            "code_version"}
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)?")


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    monkeypatch.setattr(audit, "_proc", {"kind": None, "claude_session": None})
    monkeypatch.setattr(audit, "_seen", {})


def _subparsers():
    p = build_parser()
    return next(a for a in p._actions if a.__class__.__name__ == "_SubParsersAction").choices


# ------------------------------------------------------------ the map
async def test_every_state_changing_tool_has_a_cli_form():
    """A new write tool without a CLI form fails here: a stale server's agent would have no way to act."""
    writes = {c.name for c in commands.REGISTRY if c.name and c.write}
    assert set(stale_guard.CLI) - {"log_decision"} == writes


def test_each_cli_form_is_a_real_command_with_its_required_flags():
    subs = _subparsers()
    for tool, form in stale_guard.CLI.items():
        cmd, *rest = form.split()
        assert cmd in subs, (tool, cmd)
        opts = {o for a in subs[cmd]._actions for o in a.option_strings}
        flags = {t.strip("[]") for t in rest if t.strip("[]").startswith("--")}
        assert flags <= opts, (tool, flags - opts)
        required = {a.option_strings[0] for a in subs[cmd]._actions if a.required and a.option_strings}
        assert required <= flags, (tool, required - flags)


async def test_a_refusal_names_the_cli_command(monkeypatch):
    monkeypatch.setattr(stale_guard, "_disk", lambda: NEW)
    for tool, form in stale_guard.CLI.items():
        stale = await stale_guard.refuse(tool, stale_guard.Verdict(NEW), form)
        unverified = await stale_guard.refuse(tool, stale_guard.Verdict(error="OSError: gone"), form)
        for res in (stale, unverified):
            assert f"`.venv/bin/shortbot {form}`" in res["error"] and res["error"].endswith("Nothing was sent.")
    res = await TOOLS["move_stop"]("QNT", 1.1, "trail")   # end to end: a refused write
    assert "run the same action through the CLI" in res["error"]
    assert "`.venv/bin/shortbot move-stop SYMBOL NEW_STOP --reason REASON`" in res["error"]


def test_flatten_and_kill_switch_are_cli_commands():
    subs = _subparsers()
    assert {"flatten", "kill-switch"} <= set(subs)   # registered by killswitch_ops.add_cli


def test_the_new_commands_are_logged_writes():
    assert not {"update-plan", "set-params", "reset-params", "update-learning", "start-session"} & cli.READS


# ------------------------------------------------------------ parity: MCP and CLI leave the same journal effect
def _app(tmp_path, name):
    j = Journal(tmp_path / f"{name}.db")
    s = Settings("demo", "k", "s", str(tmp_path / f"{name}.db"), "", "", RiskLimits(), ResearchBounds(), False)
    return SimpleNamespace(journal=j, settings=s, params=ResearchParams(s, j), ex=SimpleNamespace(experiment=None))


def _norm(v):
    if isinstance(v, str):
        try:
            return _norm(json.loads(v)) if v[:1] in "[{" else _TIME.sub("<t>", v)
        except ValueError:
            return _TIME.sub("<t>", v)
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items() if k not in VOLATILE}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, float) and 1e9 < v < 1e13:
        return "<t>"
    return v


def _effect(j: Journal) -> dict:
    """Every journal row, without ids, times and who acted; tool / CLI call rows aside."""
    out = {}
    for (t,) in j.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        rows = j._rows(f"SELECT * FROM {t}")
        if t == "decisions":
            rows = [r for r in rows if r["action"] != "call"]
        if t == "kv":
            rows = [r for r in rows if not r["k"].startswith("proc:")]
        out[t] = sorted((json.dumps(_norm(r), sort_keys=True, default=str) for r in rows))
    return out


async def _both(tmp_path, monkeypatch, seed, mcp_call, argv):
    """(effect after the MCP tool, effect after the CLI command, each path's call row), from identical journals."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    apps = {k: _app(tmp_path, k) for k in ("mcp", "cli")}
    for a in apps.values():
        seed(a)
    before = _effect(apps["mcp"].journal)
    assert before == _effect(apps["cli"].journal)
    for mod in (mcp_trade, mcp_research):
        monkeypatch.setattr(mod, "get_app", lambda: apps["mcp"])
    res_mcp = await mcp_call()
    for mod in (mcp_trade, mcp_research, cli):
        monkeypatch.setattr(mod, "get_app", lambda *a, **k: apps["cli"])

    async def no_close():
        return None
    monkeypatch.setattr(cli, "close_app", no_close)
    res_cli = await cli._audited(build_parser().parse_args(argv))
    effects = {k: _effect(a.journal) for k, a in apps.items()}
    assert effects["mcp"] == effects["cli"] and effects["mcp"] != before
    calls = {k: [(r["actor"], json.loads(r["snapshot"])["outcome"]) for r in a.journal.decisions(calls=True)
                 if r["action"] == "call"]
             for k, a in apps.items()}
    return res_mcp, res_cli, calls


def _open_trade(app):
    j = app.journal
    tid = j.create_trade("QNT/USDT:USDT", "pump_fade", "turned", 1.1, 2, plan={"time_stop_hours": 24})
    j.activate_trade(tid, 1.0, 100.0, 100.0)


async def test_update_plan_parity(tmp_path, monkeypatch):
    changes = {"time_stop_hours": 30, "auto": {"time_stop": True}}
    res_mcp, res_cli, calls = await _both(
        tmp_path, monkeypatch, _open_trade, lambda: TOOLS["update_plan"]("QNT", changes, "longer hold"),
        ["update-plan", "QNT", "--changes", json.dumps(changes), "--reason", "longer hold"])
    assert res_mcp["ok"] and res_mcp == res_cli
    assert calls == {"mcp": [("mcp:update_plan", "ok")], "cli": [("cli:update-plan", "ok")]}


async def test_set_and_reset_params_parity(tmp_path, monkeypatch):
    res_mcp, res_cli, calls = await _both(
        tmp_path, monkeypatch, lambda a: None,
        lambda: TOOLS["set_risk_params"]({"max_leverage": 5, "max_positions": 4}, "tight-stop fades", "lev5"),
        ["set-params", "max_leverage=5", "max_positions=4", "--experiment", "lev5", "--reason", "tight-stop fades"])
    assert res_mcp["changed"]["max_leverage"]["to"] == 5 and _norm(res_mcp) == _norm(res_cli)
    assert calls == {"mcp": [("mcp:set_risk_params", "ok")], "cli": [("cli:set-params", "ok")]}

    def lev5(a):
        a.params.set({"max_leverage": 5}, "tight-stop fades", "lev5")
    res_mcp, res_cli, calls = await _both(
        tmp_path / "reset", monkeypatch, lev5, lambda: TOOLS["reset_risk_params"]("back to baseline"),
        ["reset-params", "--reason", "back to baseline"])
    assert _norm(res_mcp) == _norm(res_cli)
    assert calls["cli"] == [("cli:reset-params", "ok")]


def test_set_params_rejects_a_malformed_pair():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["set-params", "max_leverage", "--experiment", "x", "--reason", "y"])


async def test_update_learning_parity(tmp_path, monkeypatch):
    def learning(a):
        a.journal.add_learning("pumps fade", "trades 1-3", None, "hypothesis", "low", ["trade:1"], kind="statistical")
    res_mcp, res_cli, calls = await _both(
        tmp_path, monkeypatch, learning,
        lambda: TOOLS["update_learning"](1, status="supported", evidence="trades 4-9", confidence="medium",
                                             provenance=["trade:9", "research_report"], n=31, days=12),
        ["update-learning", "1", "--status", "supported", "--evidence", "trades 4-9", "--confidence", "medium",
         "--provenance", "trade:9", "--provenance", "research_report", "--n", "31", "--days", "12"])
    assert res_mcp == res_cli == {"ok": True}
    assert calls["cli"] == [("cli:update-learning", "ok")]


async def test_start_session_parity(tmp_path, monkeypatch):
    def alerts(a):
        a.journal.log_alert("stop_near", "QNT near stop", "QNT/USDT:USDT")
    res_mcp, res_cli, calls = await _both(
        tmp_path, monkeypatch, alerts, lambda: TOOLS["start_session"]("morning"),
        ["start-session", "--notes", "morning"])
    assert res_mcp == res_cli and res_mcp["alerts_acknowledged"]["count"] == 1
    assert calls["cli"] == [("cli:start-session", "ok")]


def test_the_cli_keeps_its_own_logging_when_it_loads_a_tool_body():
    """Importing the MCP modules runs MCPServer's logging.basicConfig (stderr, INFO); a CLI process keeps its own
    root logger (no handlers: lastResort with redaction, #66). A fresh interpreter, as `shortbot` is."""
    code = ("import logging; from shortbot import cli; fn = cli.tool_body('mcp_trade', 'update_plan'); "
            "from shortbot import mcp_trade; assert fn is mcp_trade.update_plan; "
            "print(logging.root.handlers, logging.root.level)")
    env = {**os.environ, "PYTHONPATH": str(provenance.PKG.parent)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[] 30"

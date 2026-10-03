"""The command registry (#36 step 1): one declaration per command generates its MCP tool and its `shortbot` subcommand.
A declaration that can't serve both surfaces fails at import; the tool keeps the body's signature (the SDK's schemas
come from it); the parsed command line reaches the body under the body's own names; the CLI never loads the MCP SDK."""

import argparse
import inspect
import os
import subprocess
import sys

import pytest

from shortbot import commands, provenance
from shortbot.commands import Arg, Command, build_parser
from shortbot.mcp_server import TOOLS, mcp


def _cmd(name: str) -> Command:
    return commands.by_name(name)


def _parse(argv: list[str]) -> tuple[Command, dict]:
    ns = build_parser().parse_args(argv)
    cmd = commands.by_cli(ns.cmd)
    return cmd, cmd.cli_kwargs(ns)


# ------------------------------------------------------------ one declaration, both surfaces
async def test_every_tool_and_every_registered_subcommand_comes_from_one_declaration():
    names = [t.name for t in await mcp.list_tools()]
    assert names == [c.name for c in commands.REGISTRY if c.name]   # declaration order = tools/list order
    assert set(names) == set(TOOLS)
    sub = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    assert {c.cli for c in commands.REGISTRY if c.cli} <= set(sub.choices)


async def test_a_generated_tool_keeps_the_body_signature_and_output_schema():
    tools = {t.name: t for t in await mcp.list_tools()}
    for cmd in (c for c in commands.REGISTRY if c.name):
        sig = inspect.signature(TOOLS[cmd.name])
        assert sig == cmd.mcp_signature(), cmd.name
        assert list(tools[cmd.name].input_schema.get("properties", {})) == list(sig.parameters), cmd.name
        assert TOOLS[cmd.name].__doc__ == cmd.body.__doc__
    # a list / str return is wrapped as {"result": ...}: stale_guard.annotate relies on that shape (#68)
    for name in ("positions", "market_map", "latest_announcements"):
        assert list(tools[name].output_schema["properties"]) == ["result"], name
    assert "result" not in tools["account"].output_schema.get("properties", {})


def test_cli_only_args_stay_off_the_tool():
    scan = _cmd("scan_shorts")
    assert "log" in scan.sig.parameters and "log" not in scan.mcp_signature().parameters


# ------------------------------------------------------------ the command line reaches the body
def test_the_parsed_command_line_reaches_the_body_under_its_own_names():
    cmd, kw = _parse(["scan", "--min-change", "5", "--top", "3"])
    assert cmd.name == "scan_shorts"
    assert kw == {"min_change_24h": 5.0, "top_n": 3, "log": False, "direction": "gainers"}   # MCP always logs
    assert _parse(["scan", "--log"])[1]["log"] is True
    cmd, kw = _parse(["prob", "QNT", "--stop", "1.2", "--target", "1.0", "0.9", "--analyst-p", "0.4"])
    assert cmd.name == "probability"
    assert kw == {"symbol": "QNT", "stop": 1.2, "targets": [1.0, 0.9], "hours": 24.0, "entry": None,
                  "fill_hours": None, "analyst_p": 0.4, "setup": "manual"}
    assert _parse(["research"])[1] == {"min_n": 5}   # the body's default, now reachable from the CLI
    assert _parse(["log", "skip", "too thin", "--symbol", "ABC"])[1] == {
        "action": "skip", "reasoning": "too thin", "symbol": "ABC"}


def test_types_and_defaults_come_from_the_body():
    spec = {a.option_strings[0] if a.option_strings else a.dest: a
            for a in next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
            .choices["budget"]._actions}
    assert spec["--conviction"].default == "B" and spec["--conviction"].choices == ("C", "B", "A", "M")
    assert spec["symbol"].required and not spec["--setup"].required
    chart = _cmd("chart")
    assert {a.cli_dest: chart._spec(a).get("type") for a in chart.args} == {"symbol": None, "tf": None, "bars": int}


# ------------------------------------------------------------ declarations that can't serve both surfaces
def _declare(**kw):
    async def body(symbol: str, price: float, log: bool = True) -> dict:
        return {}
    body.__name__ = f"test_body_{len(commands.REGISTRY)}"
    return commands.command(**kw)(body)


@pytest.fixture
def scratch_registry(monkeypatch):
    monkeypatch.setattr(commands, "REGISTRY", list(commands.REGISTRY))
    monkeypatch.setattr(commands, "_BY_NAME", dict(commands._BY_NAME))
    monkeypatch.setattr(commands, "_BY_CLI", dict(commands._BY_CLI))


def test_a_bad_declaration_fails_at_import(scratch_registry):
    with pytest.raises(ValueError, match="lacks"):   # the subcommand can't reach `price`
        _declare(cli="t-missing", args=(Arg("symbol"),))
    with pytest.raises(ValueError, match="not a parameter"):
        _declare(cli="t-typo", args=(Arg("symbol"), Arg("price"), Arg("prise", "--prise")))
    with pytest.raises(ValueError, match="needs a default"):
        async def body(symbol: str, flag: bool) -> dict:
            return {}
        commands.command(cli="t-flag", args=(Arg("symbol"), Arg("flag", "--flag", cli_only=True)))(body)
    with pytest.raises(ValueError, match="unique"):
        _declare(cli="scan", args=(Arg("symbol"), Arg("price")))
    _declare(cli="t-ok", args=(Arg("symbol"), Arg("price"), Arg("log", "--log", action="store_true", cli_only=True)))


# ------------------------------------------------------------ the CLI never loads the MCP SDK
def test_building_the_cli_loads_neither_the_mcp_sdk_nor_its_logging():
    """A fresh interpreter, as `shortbot` is: the parser imports every declaration module, none of which may import the
    SDK (~0.3 s per CLI call) or build the MCPServer, whose logging.basicConfig would replace the CLI's own root logger
    (no handlers: lastResort with redaction, #66)."""
    code = ("import logging, sys; from shortbot import cli, commands; commands.build_parser(); "
            "assert not [m for m in sys.modules if m == 'mcp' or m.startswith('mcp.')], 'mcp loaded'; "
            "print(logging.root.handlers, logging.root.level)")
    env = {**os.environ, "PYTHONPATH": str(provenance.PKG.parent)}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[] 30"


# ------------------------------------------------------------ one body: what the CLI gained from the MCP tool
def test_the_weight_classes_agree_on_both_surfaces():
    """#70: one class per command. The CLI tagged set-params protective while MCP left set_risk_params a desk read, and
    MCP tagged flatten_all / halt_trading protective while `flatten` and `kill-switch` waited for budget like reads."""
    from shortbot.weight import PROTECTIVE
    assert _cmd("set_risk_params").weight == PROTECTIVE and _cmd("set_risk_params").cli == "set-params"
    assert _cmd("flatten_all").weight == PROTECTIVE and _cmd("flatten_all").cli == "flatten"
    assert commands.by_cli("kill-switch").weight == PROTECTIVE


def test_learn_requires_provenance_and_kind_as_add_learning_does():
    learn = ["learn", "pumps fade", "--evidence", "trades 1-3"]
    for missing in (["--kind", "statistical"], ["--provenance", "trade:1"]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(learn + missing)
    cmd, kw = _parse(learn + ["--provenance", "trade:1", "--provenance", "research_report", "--kind", "statistical"])
    assert cmd.name == "add_learning" and kw["provenance"] == ["trade:1", "research_report"]


def test_every_subcommand_help_renders():
    """`shortbot unlock-add --help` crashed: argparse read the % in "% of circulating supply" as a format code."""
    sub = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    for name, p in sub.choices.items():
        assert p.format_help(), name


async def test_scan_from_the_cli_labels_its_research_rows_with_the_regime(monkeypatch):
    """One body: `scan --log` passes the regime to the research rows as scan_shorts does (they were unlabelled)."""
    from types import SimpleNamespace

    from shortbot import mcp_trade
    seen = {}

    async def market_regime(ex):
        return {"verdict_for_shorts": "caution"}

    async def scan(ex, **kw):
        seen.update(kw)
        return {"candidates": []}
    monkeypatch.setattr(mcp_trade, "get_app", lambda: SimpleNamespace(ex=None, journal="J"))
    monkeypatch.setattr(mcp_trade, "regime", SimpleNamespace(market_regime=market_regime))
    monkeypatch.setattr(mcp_trade, "scanner", SimpleNamespace(scan_shorts=scan))
    cmd, kw = _parse(["scan", "--log"])
    assert await cmd.body(**kw) == {"regime": "caution", "candidates": []}
    assert (seen["regime"], seen["journal"]) == ("caution", "J")
    await cmd.body(**_parse(["scan"])[1])
    assert seen["journal"] is None   # without --log nothing is logged; the MCP tool always logs


async def test_one_body_keeps_who_acted_and_normalises_symbols(tmp_path, monkeypatch):
    """flatten_all / `flatten` share a body; the kill-switch row still says whether the agent's MCP or the CLI flattened.
    log_decision / `log` share one: both store the ccxt symbol (MCP stored what it was given) and answer ok."""
    from types import SimpleNamespace

    from shortbot import audit, cli, killswitch_ops, mcp_kill, mcp_research
    from shortbot.journal import Journal
    monkeypatch.setattr(audit, "_proc", {"kind": None, "claude_session": None})
    seen = []

    async def flatten(tr, by, reason, lesson=""):
        seen.append(by)
        return {"ok": True}
    j = Journal(tmp_path / "j.db")
    app = SimpleNamespace(trader=None, journal=j, settings=SimpleNamespace(mode="demo"))
    monkeypatch.setattr(killswitch_ops, "flatten", flatten)
    for mod in (mcp_kill, mcp_research, cli):
        monkeypatch.setattr(mod, "get_app", lambda *a, **k: app)

    async def no_close():
        return None
    monkeypatch.setattr(cli, "close_app", no_close)
    await TOOLS["flatten_all"]("squeeze")
    await cli._audited(build_parser().parse_args(["flatten", "--reason", "squeeze"]))
    assert seen == [mcp_kill.MCP_BY, killswitch_ops.CLI_BY]
    mcp_res = await TOOLS["log_decision"]("watch", "lower high", "QNT")
    cli_res = await cli._audited(build_parser().parse_args(["log", "skip", "too thin", "--symbol", "qntusdt"]))
    assert mcp_res["ok"] and cli_res["ok"]
    assert [d["symbol"] for d in j.decisions() if d["action"] in ("watch", "skip")] == ["QNT/USDT:USDT"] * 2

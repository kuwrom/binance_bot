"""The agent-facing surface: every MCP tool (name, approval meta, parameters), the server instructions, every CLI
subcommand with its arguments, and how each command is classified (commands.py: read / write, #70 weight class, #68
stale exemption). A refactor must leave tests/surface.json untouched; an intended change regenerates it:
SNAPSHOT_UPDATE=1 .venv/bin/pytest -q tests/test_surface.py"""

import argparse
import json
import os
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).with_name("surface.json")


class _Parsed(Exception):
    def __init__(self, parser):
        self.parser = parser


def _cli_parser(monkeypatch) -> argparse.ArgumentParser:
    """The parser `shortbot` builds, caught at parse time (works whatever module builds it)."""
    from shortbot import cli

    def catch(self, *a, **k):
        raise _Parsed(self)
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", catch)
    with pytest.raises(_Parsed) as e:
        cli.main()
    return e.value.parser


def _arg(a: argparse.Action) -> dict:
    return {"flags": a.option_strings or [a.dest], "dest": a.dest, "action": type(a).__name__,
            "required": a.required, "default": a.default, "nargs": a.nargs,
            "type": getattr(a.type, "__name__", a.type), "choices": list(a.choices) if a.choices else None}


def _cli(parser: argparse.ArgumentParser) -> dict:
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return {name: [_arg(a) for a in p._actions if not isinstance(a, argparse._HelpAction)]
            for name, p in sub.choices.items()}


async def _mcp() -> dict:
    from shortbot.mcp_server import INSTRUCTIONS, mcp
    tools = {}
    for t in await mcp.list_tools():
        props, req = t.input_schema.get("properties", {}), set(t.input_schema.get("required", []))
        tools[t.name] = {"meta": t.meta, "params": {k: ("required" if k in req else {"default": v.get("default")})
                                                   for k, v in props.items()}}
    return {"name": mcp.name, "instructions": INSTRUCTIONS, "tools": tools}


def _commands() -> dict:
    from shortbot import commands
    return {c.name or c.cli: {"mcp": c.name, "cli": c.cli, "write": c.write, "weight": c.weight,
                              "stale_exempt": c.stale_exempt, "raw": c.raw} for c in commands.load()}


def _dump(surface: dict) -> str:
    """One line per tool / subcommand so a diff shows exactly what moved."""
    def block(d: dict) -> str:
        return "{\n" + ",\n".join(f"    {json.dumps(k)}: {json.dumps(v, default=str)}" for k, v in sorted(d.items())) + "\n  }"
    m = surface["mcp"]
    return ("{\n  \"mcp_name\": " + json.dumps(m["name"]) + ",\n  \"instructions\": " + json.dumps(m["instructions"])
            + ",\n  \"tools\": " + block(m["tools"]) + ",\n  \"cli\": " + block(surface["cli"])
            + ",\n  \"commands\": " + block(surface["commands"]) + "\n}\n")


async def test_mcp_tools_and_cli_subcommands_match_the_snapshot(monkeypatch):
    surface = {"mcp": await _mcp(), "cli": _cli(_cli_parser(monkeypatch)), "commands": _commands()}
    now = _dump(surface)
    if os.environ.get("SNAPSHOT_UPDATE"):
        SNAPSHOT.write_text(now)
    want = json.loads(SNAPSHOT.read_text())
    got = json.loads(now)
    for key in ("mcp_name", "instructions"):
        assert got[key] == want[key], key
    for key in ("tools", "cli", "commands"):
        assert sorted(got[key]) == sorted(want[key]), f"{key}: names differ"
        for name in want[key]:
            assert got[key][name] == want[key][name], f"{key}/{name} changed"
    human = sorted(n for n, t in got["tools"].items() if (t["meta"] or {}).get("anthropic/requiresUserInteraction"))
    assert human == [], f"owner decision 2026-10-02: no per-call approval gates, found {human}"

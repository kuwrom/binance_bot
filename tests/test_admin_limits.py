"""`shortbot-admin limits`: the human sets the desk's risk limits (validated, code typed on a real terminal, journalled
and alerted, per desk), and no other code path writes them (the AI can't raise a limit)."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shortbot import admin
from shortbot import desk_limits as dl
from shortbot.config import RiskLimits, Settings
from shortbot.journal import Journal
from tests.test_admin import Tty

DEMO = Settings("demo", "", "", ":memory:", "", "", RiskLimits())
LIVE = Settings("live", "", "", ":memory:", "", "", RiskLimits())


def args(action, *items, reason="owner decision: smaller live size"):
    return SimpleNamespace(cmd="limits", action=action, items=list(items), reason=reason)


@pytest.fixture(autouse=True)
def human(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(admin, "_tty", lambda: Tty())


async def test_set_validates_names_types_and_sanity_before_any_prompt(monkeypatch):
    j = Journal(":memory:")
    prompts = []
    monkeypatch.setattr(admin, "_tty", lambda: prompts.append(1) or Tty())
    for items, why in ((("bogus=1",), "unknown limit"), (("max_leverage=2.5",), "whole number"),
                       (("max_leverage=0",), "positive"), (("daily_loss_limit_pct=120",), "<= 100"),
                       (("max_total_equity_x=30",), "<= 20"), (("max_leverage",), "KEY=VALUE"),
                       (("max_position_notional=900",), "max_position_notional must be <= max_total_notional")):
        r = await admin.run(args("set", *items), DEMO, j)
        assert not r["ok"] and why in r["error"], (items, r)
    assert "--reason" in (await admin.run(args("set", "max_leverage=2", reason=" "), DEMO, j))["error"]
    assert prompts == [] and dl.stored(j) == {}


async def test_set_needs_the_code_then_writes_journals_and_alerts(monkeypatch):
    j = Journal(":memory:")
    monkeypatch.setattr(admin, "_tty", lambda: Tty("ZZZZ"))
    assert "did not match" in (await admin.run(args("set", "max_leverage=2"), DEMO, j))["error"]
    assert dl.stored(j) == {}
    monkeypatch.setenv("CLAUDECODE", "1")
    assert "Claude Code" in (await admin.run(args("set", "max_leverage=2"), DEMO, j))["error"]
    monkeypatch.delenv("CLAUDECODE")
    monkeypatch.setattr(admin, "_tty", lambda: Tty())
    r = await admin.run(args("set", "MAX_LEVERAGE=2", "max_total_equity_x=1.5", "Risk_Per_Trade_Pct=0.5"), DEMO, j)
    assert r["ok"], r
    assert dl.stored(j) == {"max_leverage": 2, "max_total_equity_x": 1.5, "risk_per_trade_pct": 0.5}
    assert isinstance(dl.stored(j)["max_leverage"], int)
    assert "max_leverage: unset -> 2" in Tty.shown and "[DEMO]" in Tty.shown
    d = j.decisions()[0]
    snap = json.loads(d["snapshot"])
    assert d["action"] == "desk_limits_changed" and snap["by"].startswith("human:")
    assert snap["diff"]["max_leverage"] == {"from": None, "to": 2}
    a = j.alerts()[0]
    assert a["kind"] == "desk_limits_changed" and "human:" in a["message"] and "max_leverage unset -> 2" in a["message"]
    assert dl.baseline(DEMO, j).max_leverage == 2


async def test_unset_drops_back_to_env_and_status_shows_sources(monkeypatch):
    monkeypatch.setenv("MAX_POSITIONS", "4")
    s = Settings("demo", "", "", ":memory:", "", "", RiskLimits.from_env())
    j = Journal(":memory:")
    assert (await admin.run(args("set", "max_positions=2", "max_leverage=2"), s, j))["ok"]
    st = await admin.run(args("status"), s, j)
    assert st["limits"]["max_positions"] == {"value": 2, "source": "desk"}
    assert st["limits"]["max_adds"]["source"] == "default"
    r = await admin.run(args("unset", "MAX_POSITIONS"), s, j)
    assert r["ok"] and "back to .env/default 4" in Tty.shown and dl.stored(j) == {"max_leverage": 2}
    st = await admin.run(args("status"), s, j)
    assert st["limits"]["max_positions"] == {"value": 4, "source": ".env"}
    assert "nothing would change" in (await admin.run(args("unset", "max_positions"), s, j))["error"]
    assert "names only" in (await admin.run(args("unset", "max_leverage=3"), s, j))["error"]


async def test_live_and_demo_desks_are_separate(tmp_path):
    jd, jl = Journal(tmp_path / "j.db"), Journal(tmp_path / "j.db", "live")
    assert (await admin.run(args("set", "max_position_notional=20"), LIVE, jl))["ok"]
    assert dl.baseline(LIVE, jl).max_position_notional == 20
    assert dl.baseline(DEMO, jd).max_position_notional == 100 and dl.stored(jd) == {}
    assert "[LIVE]" in Tty.shown


async def test_a_change_while_the_human_types_wins(monkeypatch):
    j = Journal(":memory:")

    class Busy(Tty):
        def readline(self, *a):
            j.set(dl.key(j), json.dumps({"max_leverage": 1}))
            return super().readline()
    monkeypatch.setattr(admin, "_tty", lambda: Busy())
    r = await admin.run(args("set", "max_leverage=2"), DEMO, j)
    assert not r["ok"] and "changed while you were confirming" in r["error"] and dl.stored(j) == {"max_leverage": 1}


def test_the_parser_takes_limits():
    a = admin.parser().parse_args(["--mode", "live", "limits", "set", "MAX_LEVERAGE=2", "--reason", "r"])
    assert (a.mode, a.cmd, a.action, a.items, a.reason) == ("live", "limits", "set", ["MAX_LEVERAGE=2"], "r")


# ------------------------------------------------------------ no other writer
SRC = Path(__file__).resolve().parents[1] / "src" / "shortbot"
WRITERS = {"desk_limits.py", "admin_limits.py"}   # the store itself and shortbot-admin's subcommand
ADMIN_ONLY = {"admin.py"}                           # the human console: the only importer of admin_limits


def test_no_non_admin_code_writes_desk_limits():
    """Only shortbot-admin reaches desk_limits.write or the kv key: never the MCP server, the `shortbot` CLI, the
    gateway or the guardian. A module that names the key, calls write, or imports admin_limits fails here."""
    bad = []
    for path in SRC.rglob("*.py"):
        if path.name in WRITERS:
            continue
        text = path.read_text()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == dl.KV:
                bad.append(f"{path.name}: names the kv key {dl.KV!r}")
            if isinstance(node, ast.Attribute) and node.attr in ("write", "KV", "key") and \
                    isinstance(node.value, ast.Name) and node.value.id in ("dl", "desk_limits"):
                bad.append(f"{path.name}: desk_limits.{node.attr}")
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("desk_limits") and \
                    {a.name for a in node.names} & {"write", "KV", "key"}:
                bad.append(f"{path.name}: imports desk_limits write/KV/key")
            if isinstance(node, (ast.Import, ast.ImportFrom)) and path.name not in ADMIN_ONLY and (
                    "admin_limits" in (getattr(node, "module", None) or "")
                    or any("admin_limits" in a.name for a in node.names)):
                bad.append(f"{path.name}: imports admin_limits")
    assert bad == []


def test_mcp_and_cli_commands_have_no_desk_limit_writer():
    """Every registered command (MCP tool and `shortbot` subcommand) by name and module: none is a limits writer."""
    from shortbot import commands
    cmds = commands.load()
    assert cmds
    mods = {c.body.__module__.rsplit(".", 1)[-1] for c in cmds}
    assert not mods & {Path(w).stem for w in WRITERS | ADMIN_ONLY}
    assert not [c.name for c in cmds if "desk_limit" in (c.name or "") or "desk_limit" in (c.cli or "")]

"""shortbot-admin: the human-only side of the kill switch. The clear (with the hold it shows and stores) and the
equity-peak reset both need a code typed on a real terminal and swap only what the human saw."""

import io
import json
import re
from types import SimpleNamespace

import pytest

from shortbot import admin
from shortbot import equity_marks as em
from shortbot import killswitch as ks
from shortbot.journal import Journal
from shortbot.killswitch import ACTIVE, HALTED, REDUCING
from tests.test_killswitch import SETTINGS


class Tty(io.StringIO):
    """A terminal whose human types `answer`, or reads the code off the screen and types it."""
    shown = ""

    def __init__(self, answer=None):
        super().__init__()
        self.answer = answer

    def readline(self, *a):
        Tty.shown = self.getvalue()
        return (self.answer or re.search(r"type (\w{4}) to confirm", self.getvalue()).group(1)) + "\n"


def clear_args(to=ACTIVE, margin=1.0):
    return SimpleNamespace(cmd="clear", to=to, reason="reviewed the trip", rearm_margin=margin)


def peak_args(reason="demo top-up reviewed"):
    return SimpleNamespace(cmd="equity-peak", action="reset", reason=reason)


async def equity_9k(settings):
    return 9_000.0


@pytest.fixture(autouse=True)
def human(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)


# ------------------------------------------------------------ clear
async def test_admin_clear_needs_a_human_at_a_real_terminal(monkeypatch):
    j = Journal(":memory:")
    await ks.trip(j, SETTINGS, HALTED, "guardian", "drawdown")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert "Claude Code" in (await admin.run(clear_args(), SETTINGS, j))["error"]
    monkeypatch.delenv("CLAUDECODE")

    def no_tty():
        raise OSError(6, "Device not configured")
    monkeypatch.setattr(admin, "_tty", no_tty)
    assert "interactive terminal" in (await admin.run(clear_args(), SETTINGS, j))["error"]
    monkeypatch.setattr(admin, "_tty", lambda: Tty("ABCD"))
    assert "did not match" in (await admin.run(clear_args(), SETTINGS, j))["error"]
    assert ks.read(j)["state"] == HALTED
    monkeypatch.setattr(admin, "_tty", lambda: Tty())
    r = await admin.run(clear_args(REDUCING), SETTINGS, j)
    assert r["ok"] and ks.read(j)["state"] == REDUCING and ks.read(j)["by"].startswith("human:")
    assert "not a loosening" in (await admin.run(clear_args(REDUCING), SETTINGS, j))["error"]


async def test_admin_clear_fails_if_a_trip_lands_while_the_human_types(monkeypatch):
    j = Journal(":memory:")
    await ks.trip(j, SETTINGS, REDUCING, "guardian", "daily loss -3.1%", cause="daily_loss")

    class Busy(Tty):   # another process escalates between the prompt and the answer
        def readline(self, *a):
            j.set(ks.KV_KEY, json.dumps({"state": HALTED, "by": "agent (MCP)", "reason": "squeeze", "at": "t",
                                         "cause": "manual"}))
            return super().readline()
    monkeypatch.setattr(admin, "_tty", lambda: Busy())
    r = await admin.run(clear_args(), SETTINGS, j)
    assert not r["ok"] and "re-run" in r["error"] and ks.read(j)["reason"] == "squeeze"
    monkeypatch.setattr(admin, "_tty", lambda: Tty())   # the re-run shows (and clears) the new state
    assert (await admin.run(clear_args(), SETTINGS, j))["ok"] and ks.read(j)["state"] == ACTIVE


async def test_admin_clear_shows_the_hold_and_stores_what_was_shown(monkeypatch):
    j = Journal(":memory:")
    await ks.trip_on_drawdown(j, SETTINGS, 13.0)
    monkeypatch.setattr(admin, "_tty", lambda: Tty())
    r = await admin.run(clear_args(margin=1.5), SETTINGS, j)
    assert "drawdown 13.00%" in Tty.shown and "14.50%" in Tty.shown and "daily loss" in Tty.shown
    assert r["ok"] and r["rearm"]["held"]["drawdown"]["rearm_at_pct"] == 14.5
    assert json.loads(j.decisions()[0]["snapshot"])["rearm"]["margin_pp"] == 1.5
    assert await ks.trip_on_drawdown(j, SETTINGS, 14.0) is None


async def test_admin_clear_refuses_an_out_of_bounds_margin(monkeypatch):
    j = Journal(":memory:")
    await ks.trip(j, SETTINGS, REDUCING, "guardian", "drawdown")
    monkeypatch.setattr(admin, "_tty", lambda: Tty())
    assert "margin" in (await admin.run(clear_args(margin=50), SETTINGS, j))["error"]
    assert ks.read(j)["state"] == REDUCING


# ------------------------------------------------------------ equity-peak reset
async def test_peak_reset_needs_a_human_at_a_real_terminal(monkeypatch):
    j = Journal(":memory:")
    j.set(j.mode_key(em.KV_PEAK), "10500.0")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert "Claude Code" in (await admin.run(peak_args(), SETTINGS, j, equity_9k))["error"]
    monkeypatch.delenv("CLAUDECODE")
    monkeypatch.setattr(admin, "_tty", lambda: Tty("ABCD"))
    assert "did not match" in (await admin.run(peak_args(), SETTINGS, j, equity_9k))["error"]
    assert "reason" in (await admin.run(peak_args(" "), SETTINGS, j, equity_9k))["error"]
    assert j.get(j.mode_key(em.KV_PEAK)) == "10500.0" and j.decisions() == []


async def test_peak_reset_moves_the_peak_to_live_equity_and_is_journalled(monkeypatch):
    j = Journal(":memory:", "live")                       # the live account's peak (journal.mode_key), not demo's
    j.set(em.KV_PEAK, "20000.0")
    j.set(j.mode_key(em.KV_PEAK), "10500.0")
    monkeypatch.setattr(admin, "_tty", lambda: Tty())
    r = await admin.run(peak_args(), SETTINGS, j, equity_9k)
    assert "10500" in Tty.shown and "9000.00" in Tty.shown and "14.3%" in Tty.shown
    assert r["ok"] and r["old_peak"] == 10_500 and r["new_peak"] == 9_000
    assert float(j.get(j.mode_key(em.KV_PEAK))) == 9_000 and j.get(em.KV_PEAK) == "20000.0"
    d = j.decisions()[0]
    assert d["action"] == "equity_peak_reset" and "human:" in d["reasoning"] and "demo top-up" in d["reasoning"]
    assert j.alerts()[0]["kind"] == "equity_peak_reset"


async def test_peak_reset_fails_if_the_peak_moves_while_the_human_types(monkeypatch):
    j = Journal(":memory:")
    j.set(j.mode_key(em.KV_PEAK), "10500.0")

    class Busy(Tty):   # the guardian records a new high (or a transfer) between the prompt and the answer
        def readline(self, *a):
            j.set(j.mode_key(em.KV_PEAK), "10600.0")
            return super().readline()
    monkeypatch.setattr(admin, "_tty", lambda: Busy())
    r = await admin.run(peak_args(), SETTINGS, j, equity_9k)
    assert not r["ok"] and "re-run" in r["error"] and j.get(j.mode_key(em.KV_PEAK)) == "10600.0"
    assert j.decisions() == []


async def test_peak_reset_is_human_only_never_the_shortbot_cli_or_mcp():
    from shortbot.commands import build_parser
    from shortbot.mcp_server import mcp
    assert not [x.name for x in await mcp.list_tools() if "peak" in x.name or "clear" in x.name]
    sub = next(a for a in build_parser()._actions if a.dest == "cmd")
    assert not [c for c in sub.choices if "peak" in c or "clear" in c]
    args = admin.parser().parse_args(["equity-peak", "reset", "--reason", "x"])
    assert (args.cmd, args.action) == ("equity-peak", "reset")
    assert admin.parser().parse_args(["clear", "--reason", "x"]).rearm_margin == 1.0


def test_confirm_reads_the_code_from_a_real_terminal(monkeypatch):
    """The real _tty on a pseudo-terminal (every other test fakes it): a terminal isn't seekable, so "r+" text mode
    refused it on the desk ("File or stream is not seekable", 2026-10-04) and on a Mac alike."""
    import os
    import re as _re
    import threading
    pty = pytest.importorskip("pty")
    monkeypatch.delenv("CLAUDECODE", raising=False)
    master, slave = pty.openpty()
    monkeypatch.setattr(admin, "TTY", os.ttyname(slave))
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(why=admin.confirm("clear the kill switch")), daemon=True)
    t.start()
    seen = b""
    while not (m := _re.search(rb"type (\w{4}) to confirm", seen)):
        seen += os.read(master, 1024)
    os.write(master, m.group(1) + b"\n")
    t.join(5)
    os.close(master)
    os.close(slave)
    assert not t.is_alive() and out == {"why": None}, out

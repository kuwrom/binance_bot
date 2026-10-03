"""shortbot-admin ladder-clear / ladder-status (#67): the human side of the setup ladder. The clear re-enables a
SHADOWed setup only when the criteria frozen at its stop pass on the shadow evidence (code-evaluated); the human's
code is the sign-off live mode needs, never an override. Same gate as the kill-switch clear."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from shortbot import admin, audit
from shortbot import setup_ladder as L
from shortbot.journal import Journal
from tests.test_admin import Tty
from tests.test_killswitch import SETTINGS
from tests.test_setup_ladder import GOOD, rows

KEY = "setup_ladder:pump_fade"


def shadowed(mode="demo", episodes=GOOD):
    """A journal whose pump_fade ladder hit its stop, with these labelled shadow episodes since."""
    j = Journal(":memory:", mode)
    rs = rows([2] + [-1] * 13)
    j.closed_results = lambda all_modes=False: rs
    cands = [{"setups": ["pump_fade"], "short_ret_72h": e["ret"], "ts": e["day"] + "T06:00"} for e in episodes]
    j.candidates = lambda labelled_only=False, limit=5000: cands
    assert L.status(j, "pump_fade")["state"] == "SHADOW"
    return j


def clear(setup="pump_fade", reason="reviewed the shadow evidence"):
    return SimpleNamespace(cmd="ladder-clear", setup=setup, reason=reason)


def ladder_clears(j):
    return [d for d in j.decisions() if d["action"] == "setup_ladder_clear"]


def never_prompted():
    pytest.fail("prompted although nothing could change")


@pytest.fixture(autouse=True)
def human(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)


async def test_ladder_clear_needs_a_human_at_a_real_terminal(monkeypatch):
    j = shadowed()
    before = j.get(KEY)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert "Claude Code" in (await admin.run(clear(), SETTINGS, j))["error"]
    monkeypatch.delenv("CLAUDECODE")

    def no_tty():
        raise OSError(6, "Device not configured")
    monkeypatch.setattr(admin, "_tty", no_tty)
    assert "interactive terminal" in (await admin.run(clear(), SETTINGS, j))["error"]
    monkeypatch.setattr(admin, "_tty", lambda: Tty("ABCD"))
    assert "did not match" in (await admin.run(clear(), SETTINGS, j))["error"]
    assert "reason" in (await admin.run(clear(reason=" "), SETTINGS, j))["error"]
    assert j.get(KEY) == before and ladder_clears(j) == [] and L.status(j, "pump_fade")["state"] == "SHADOW"


async def test_ladder_clear_with_the_evidence_met_reenables_at_half_journals_and_alerts(monkeypatch):
    j = shadowed()
    monkeypatch.setattr(admin, "_tty", lambda: Tty())
    args = admin.parser().parse_args(["ladder-clear", "--setup", "pump_fade", "--reason", "reviewed the evidence"])
    with audit.acting(f"admin:{args.cmd}"):                  # what main() wraps every command in
        r = await admin.run(args, SETTINGS, j)
    assert r["ok"] and r["n"] == 30 and r["days"] == 10 and r["reenabled_at"]
    assert "30 episodes on 10 days" in Tty.shown and "HALF" in Tty.shown and "13.0R" in Tty.shown
    assert L.status(j, "pump_fade")["state"] == "HALF"
    d = ladder_clears(j)[0]
    assert d["actor"] == "admin:ladder-clear" and "human:" in d["reasoning"]
    assert "reviewed the evidence" in d["reasoning"]
    snap = json.loads(d["snapshot"])
    assert snap["evidence"]["ok"] and snap["mode"] == "demo"
    assert snap["stop"]["at"] == rows([2] + [-1] * 13)[13]["closed_at"]
    a = [x for x in j.alerts() if x["kind"] == "setup_ladder_clear"]
    assert len(a) == 1 and "pump_fade" in a[0]["message"] and a[0]["actor"] == "admin:ladder-clear"
    assert json.loads(j.get(KEY))["log"][-1]["human"] is True
    assert "not stopped" in (await admin.run(clear(), SETTINGS, j))["error"]   # nothing left to clear


async def test_ladder_clear_in_live_is_the_human_sign_off_on_a_code_pass(monkeypatch):
    j = shadowed("live")
    assert j.get(KEY + "@live") and j.get(KEY) is None       # the live account's own record
    monkeypatch.setattr(admin, "_tty", lambda: Tty())
    r = await admin.run(clear(), replace(SETTINGS, mode="live"), j)
    assert r["ok"] and L.status(j, "pump_fade")["state"] == "HALF"
    assert json.loads(ladder_clears(j)[0]["snapshot"])["mode"] == "live"


async def test_ladder_clear_refuses_when_the_frozen_criteria_fail_and_says_why(monkeypatch):
    j = shadowed(episodes=GOOD[:29])
    before = j.get(KEY)
    monkeypatch.setattr(admin, "_tty", never_prompted)
    r = await admin.run(clear(), SETTINGS, j)
    assert not r["ok"] and "frozen re-enable criteria not met" in r["error"] and "29 episodes < 30" in r["error"]
    assert r["evidence"]["n"] == 29 and j.get(KEY) == before and ladder_clears(j) == []
    bad = shadowed(episodes=[{**e, "ret": -0.1} for e in GOOD])
    assert "mean -0.1 <= 0.0" in (await admin.run(clear(), SETTINGS, bad))["error"]


async def test_ladder_clear_of_a_setup_that_is_not_stopped_changes_nothing(monkeypatch):
    j = Journal(":memory:")
    monkeypatch.setattr(admin, "_tty", never_prompted)
    r = await admin.run(clear("funding_hi"), SETTINGS, j)
    assert not r["ok"] and "not stopped" in r["error"] and j.get("setup_ladder:funding_hi") is None


async def test_ladder_clear_fails_if_the_ladder_changes_while_the_human_types(monkeypatch):
    j = shadowed()

    class Busy(Tty):   # another process writes the ladder row between the prompt and the answer
        def readline(self, *a):
            L.reset_epoch(j, "pump_fade", "agent", "rule change")
            return super().readline()
    monkeypatch.setattr(admin, "_tty", lambda: Busy())
    r = await admin.run(clear(), SETTINGS, j)
    assert not r["ok"] and "re-run" in r["error"] and json.loads(j.get(KEY))["reenables"] == []
    assert L.status(j, "pump_fade")["state"] == "SHADOW" and ladder_clears(j) == []


async def test_ladder_status_lists_every_ladder_and_the_reenable_evidence():
    j = shadowed(episodes=GOOD[:29])
    rs = rows([2] + [-1] * 13) + rows([1, 1], "funding_hi", 5)
    j.closed_results = lambda all_modes=False: rs
    r = await admin.run(SimpleNamespace(cmd="ladder-status"), SETTINGS, j)
    assert r["ok"] and r["mode"] == "demo" and set(r["setups"]) == {"pump_fade", "funding_hi"}
    pf, fh = r["setups"]["pump_fade"], r["setups"]["funding_hi"]
    assert pf["state"] == "SHADOW" and pf["mult"] == 0.0 and pf["stop"]["at"] and fh["state"] == "NORMAL"
    assert pf["reenable"]["ok"] is False and "29 episodes < 30" in pf["reenable"]["fails"]
    assert "shortbot-admin ladder-clear --setup pump_fade" in pf["reenable"]["clear"]
    assert "reenable" not in fh and not {"episodes", "raw"} & set(pf["reenable"])   # the verdict, not the rows


def test_ladder_commands_parse_and_stay_out_of_the_agent_cli():
    a = admin.parser().parse_args(["ladder-clear", "--setup", "pump_fade", "--reason", "x"])
    assert (a.cmd, a.setup, a.reason) == ("ladder-clear", "pump_fade", "x")
    assert admin.parser().parse_args(["ladder-status"]).cmd == "ladder-status"
    with pytest.raises(SystemExit):
        admin.parser().parse_args(["ladder-clear", "--setup", "pump_fade"])     # a reason is required


def reset(setup="pump_fade", reason="rules reviewed after the stop"):
    return SimpleNamespace(cmd="ladder-reset", setup=setup, reason=reason)


async def test_ladder_reset_needs_a_human_and_a_reason(monkeypatch):
    j = shadowed()
    before = j.get(KEY)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert "Claude Code" in (await admin.run(reset(), SETTINGS, j))["error"]
    monkeypatch.delenv("CLAUDECODE")
    assert "reason" in (await admin.run(reset(reason=" "), SETTINGS, j))["error"]
    monkeypatch.setattr(admin, "_tty", lambda: Tty("ABCD"))
    assert "did not match" in (await admin.run(reset(), SETTINGS, j))["error"]
    assert j.get(KEY) == before


async def test_ladder_reset_restarts_the_ledger_journals_old_values_and_keeps_the_shadow_stop(monkeypatch):
    j = shadowed()
    monkeypatch.setattr(admin, "_tty", lambda: Tty())
    args = admin.parser().parse_args(["ladder-reset", "--setup", "pump_fade", "--reason", "rules changed"])
    with audit.acting(f"admin:{args.cmd}"):
        r = await admin.run(args, SETTINGS, j)
    assert r["ok"] and r["epoch"] and r["old"]["state"] == "SHADOW" and r["state"] == "SHADOW"
    assert "SHADOW stop stays" in Tty.shown
    d = [x for x in j.decisions() if x["action"] == "setup_ladder_epoch_reset"][0]
    snap = json.loads(d["snapshot"])
    assert snap["old"]["dd_r"] is not None and snap["new_epoch"] == r["epoch"] and d["actor"] == "admin:ladder-reset"
    assert json.loads(j.get(KEY))["log"][-1]["kind"] == "epoch_reset"
    assert [a for a in j.alerts() if a["kind"] == "setup_ladder_epoch_reset"]


async def test_ladder_reset_fails_if_the_ladder_changes_while_the_human_types(monkeypatch):
    j = shadowed()

    class Busy(Tty):
        def readline(self, *a):
            L.reset_epoch(j, "pump_fade", "someone", "concurrent review")
            return super().readline()
    monkeypatch.setattr(admin, "_tty", lambda: Busy())
    r = await admin.run(reset(), SETTINGS, j)
    assert not r["ok"] and "re-run" in r["error"]
    assert [x for x in j.decisions() if x["action"] == "setup_ladder_epoch_reset"] == []

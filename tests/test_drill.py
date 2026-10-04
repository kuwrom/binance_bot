"""The kill-switch drill (#46, drill.py / drill_kill.py) against the fake: each step the real command's intent (actor
cli:drill:kill-switch; through the gateway when it is on), REDUCING / HALTED / flatten behave as the runbook says,
the run is journalled (drill_step rows, a drill_result awaiting the owner's clear), --verify records the pass only
after a shortbot-admin clear. Refusals send and journal nothing; a failed step cleans up."""

import json
from types import SimpleNamespace

import pytest

from shortbot import app as app_mod
from shortbot import audit, cli, drill, drill_kill, drill_log, uptime
from shortbot import killswitch as ks
from shortbot.commands import build_parser
from shortbot.config import RiskLimits, Settings
from shortbot.entries import EntryDesk
from shortbot.journal import Journal
from shortbot.journal_tables import DRILL_SETUP
from shortbot.trading import Trader
from tests.fakes import FakeCcxt, FakeEx, neutral_budget, ok_prechecks
from tests.gateway_rig import as_session, flag_on, served

XRP, ADA = "XRP/USDT:USDT", "ADA/USDT:USDT"


async def favorable(ex):
    return {"verdict_for_shorts": "favorable"}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    async def instant(_s):
        return None
    monkeypatch.setattr(drill, "_sleep", instant)
    monkeypatch.setattr("shortbot.market.regime.market_regime", favorable)   # EntryDesk's regime read


def desk(monkeypatch, mode="demo", ada_volume=100e6, path=":memory:"):
    fake = FakeCcxt()
    fake.add_symbol(XRP, 2.5, tick=0.0001, step=0.1, min_notional=5.0)
    fake.add_symbol(ADA, 0.8, tick=0.0001, step=1.0, min_notional=5.0, quote_volume=ada_volume)
    ex = FakeEx(fake)
    if mode == "live":
        ex.settings = Settings("live", "k", "s", ":memory:", "", "", RiskLimits())
    tr = Trader(ex, Journal(path), prechecks=ok_prechecks, budget=neutral_budget)
    app = SimpleNamespace(trader=tr, journal=tr.j, ex=ex, settings=ex.settings)
    monkeypatch.setattr(app_mod, "_app", app)
    return app, fake


def results(j):
    return [r for r in j.decisions(limit=500) if r["action"] == drill_log.RESULT]


def intent_rows(j):
    return j._rows("SELECT * FROM intents ORDER BY rowid")


async def ran(app):
    with audit.acting("cli:drill"):
        return await drill.run(app, "kill-switch", symbol="XRPUSDT", second="ADAUSDT")


# ------------------------------------------------------------ the run
async def test_the_kill_switch_drill_passes_every_check_and_waits_for_the_owner(monkeypatch):
    app, fake = desk(monkeypatch)
    j = app.journal
    res = await ran(app)
    assert res["verdict"] == "awaiting_clear", {k: c for k, c in res["checks"].items() if not c["ok"]}
    assert set(res["checks"]) >= {"opened", "one_close_stop", "entry_rests", "reducing", "reducing_open_refused",
                                  "reducing_entry_rests", "reducing_tighten_ok", "reducing_widen_refused", "halted",
                                  "halted_entry_withdrawn", "halted_stop_kept", "flatten_flat",
                                  "flatten_trade_closed", "still_halted", "completed", "nothing_left"}
    assert ks.read(j)["state"] == ks.HALTED and drill_kill.CLEAR in res["next"][0]
    assert res["override"] == drill_log.OVERRIDE and "veto_override" in res["override_note"]
    assert fake.sym[XRP]["qty"] == 0 and fake.sym[ADA]["qty"] == 0
    # every step: the real command's intent, the drill's actor, linked from its drill_step row
    rows = intent_rows(j)
    assert [r["kind"] for r in rows] == ["open_short", "propose_entries", "kill_switch", "open_short", "move_stop",
                                         "widen_stop", "kill_switch", "flatten_all"]
    assert {r["actor"] for r in rows} == {"cli:drill:kill-switch"} and res["intents"] == [r["id"] for r in rows]
    assert [r["status"] for r in rows][3] == "refused" and rows[5]["status"] == "refused"
    steps = [r for r in j.decisions(limit=500) if r["action"] == drill_log.STEP]
    assert len(steps) == 8 and {json.loads(s["snapshot"])["run"] for s in steps} == {res["run"]}
    # the drill trade: exp_drill, a full plan, the override, its lesson, and no statistic sees it
    (t,) = [t for t in j.trades(10) if t["setup"] == DRILL_SETUP]
    assert t["status"] == "closed" and t["pnl"] is not None and t["lesson"] == drill_kill.LESSON
    plan = t["plan"]
    assert plan["trigger"] and plan["invalidation"] and len(plan["targets"]) == 2 and plan["time_stop_hours"] == 3.0
    assert plan["auto"] == {"breakeven": False, "trail": False, "targets": False, "time_stop": True}
    assert j.closed_results() == [] and j.stats()["closed_trades"] == 0
    assert uptime.windows(j)["kill-switch"]["until"] <= uptime.windows(j)["kill-switch"]["from"] + 1800
    (r,) = results(j)
    assert json.loads(r["snapshot"])["verdict"] == "awaiting_clear" and r["actor"] == "cli:drill:kill-switch"


async def test_verify_records_the_pass_only_after_a_shortbot_admin_clear(monkeypatch):
    app, _ = desk(monkeypatch)
    j = app.journal
    assert (await ran(app))["verdict"] == "awaiting_clear"
    no = await drill.run(app, "kill-switch", verify=True)
    assert no["ok"] is False and no["state"] == ks.HALTED and drill_kill.CLEAR in no["error"]
    with audit.acting("admin:clear"):
        assert (await ks.clear(j, app.settings, ks.ACTIVE, "human:kib (tty)", "drill done", j.get(ks.KV_KEY)))["ok"]
    res = await drill.run(app, "kill-switch", verify=True)
    assert res["verdict"] == "pass" and res["checks"]["admin_clear"]["ok"] and res["checks"]["run:flatten_flat"]["ok"]
    latest = drill_log.latest_results(j)["kill-switch"]
    assert latest["verdict"] == "pass" and latest["verifies"] and latest["code_version"] == audit.CODE_VERSION
    assert (await drill.run(app, "kill-switch", verify=True))["already_verified"] is True


async def test_verify_fails_when_no_admin_clear_lowered_the_switch(monkeypatch):
    app, _ = desk(monkeypatch)
    j = app.journal
    await ran(app)
    j.set(ks.KV_KEY, json.dumps({"state": ks.ACTIVE, "by": "someone", "reason": "edited by hand", "at": "x"}))
    res = await drill.run(app, "kill-switch", verify=True)
    assert res["verdict"] == "fail" and not res["checks"]["admin_clear"]["ok"]


async def test_through_the_gateway_each_step_is_served_there(monkeypatch, tmp_path):
    app, _ = desk(monkeypatch, path=tmp_path / "j.db")
    app.settings = flag_on(app.settings)
    async with served(app.journal):
        as_session(monkeypatch, "cli")
        res = await ran(app)
    assert res["verdict"] == "awaiting_clear", {k: c for k, c in res["checks"].items() if not c["ok"]}
    assert {s["via"] for s in res["steps"]} == {"gateway"}
    assert {r["actor"] for r in intent_rows(app.journal)} == {"cli:drill:kill-switch"}


async def test_the_cli_runs_it_as_a_read_whose_steps_are_intents(monkeypatch):
    app, _ = desk(monkeypatch)

    async def no_close():
        return None
    monkeypatch.setattr(cli, "close_app", no_close)
    res = await cli._audited(build_parser().parse_args(["drill", "kill-switch"]))
    assert res["verdict"] == "awaiting_clear"
    assert all(r["actor"] == "cli:drill:kill-switch" for r in intent_rows(app.journal))


# ------------------------------------------------------------ a step that fails: cleaned up
async def test_an_entry_that_cannot_rest_stops_the_drill_and_closes_its_short(monkeypatch):
    app, fake = desk(monkeypatch, ada_volume=1e3)   # too thin to place a limit entry on
    res = await ran(app)
    assert res["verdict"] == "fail" and not res["checks"]["entry_rests"]["ok"] and not res["checks"]["completed"]["ok"]
    assert fake.sym[XRP]["qty"] == 0 and ks.read(app.journal)["state"] == ks.ACTIVE   # never tripped
    (t,) = app.journal.trades(5)
    assert t["status"] == "closed" and t["lesson"] == drill_kill.ABORT
    assert [s["step"] for s in res["steps"]][-1] == f"cleanup: close {XRP}"


# ------------------------------------------------------------ refusals: nothing sent, nothing journalled
async def _refused(app, fake, why):
    n = len(fake.orders)
    res = await ran(app)
    assert res["ok"] is False and res["refused"] and why in res["error"], res
    assert len(fake.orders) == n and results(app.journal) == [] and intent_rows(app.journal) == []


async def test_refuses_outside_demo(monkeypatch):
    app, fake = desk(monkeypatch, mode="live")
    await _refused(app, fake, "demo")
    app, fake = desk(monkeypatch)
    fake.urls = {"api": {"fapiPrivate": "https://fapi.binance.com/fapi/v1"}}
    await _refused(app, fake, "demo host")


async def test_refuses_unless_the_switch_is_active_and_protection_verified(monkeypatch):
    app, fake = desk(monkeypatch)
    await ks.trip(app.journal, app.settings, ks.REDUCING, "test", "drill refusal")
    await _refused(app, fake, "kill switch")
    app, fake = desk(monkeypatch)

    async def unverified(tr, now=None):
        return "protection unverified"
    monkeypatch.setattr(ks, "watch_guardian", unverified)
    await _refused(app, fake, "unverified")


async def test_refuses_a_desk_that_is_not_flat(monkeypatch):
    app, fake = desk(monkeypatch)
    app.journal.create_trade("PUMP/USDT:USDT", "pump_fade", "t", 1.1, 2)
    await _refused(app, fake, "flat desk")
    app, fake = desk(monkeypatch)
    await fake.create_order("PUMP/USDT:USDT", "market", "sell", 10)
    await _refused(app, fake, "flat desk")
    app, fake = desk(monkeypatch)
    EntryDesk(app.trader)
    app.journal._insert("INSERT INTO entries (group_id, symbol, status, created_at, expires_ms) VALUES "
                        "('g', 'PUMP/USDT:USDT', 'pending', '2026-10-04T00:00:00+00:00', 0)", ())
    await _refused(app, fake, "resting entries")


async def test_refuses_a_drill_symbol_that_is_not_clean_or_twice_the_same(monkeypatch):
    app, fake = desk(monkeypatch)
    await fake.create_order(ADA, "limit", "sell", 10, 0.9, {"timeInForce": "GTC"})   # the owner's, say
    await _refused(app, fake, "open order")
    app, fake = desk(monkeypatch)
    res = await drill.run(app, "kill-switch", symbol="XRPUSDT", second="XRPUSDT")
    assert res["refused"] and "differ" in res["error"] and fake.orders == []


# ------------------------------------------------------------ records
async def test_status_and_attest(monkeypatch):
    app, _ = desk(monkeypatch)
    j = app.journal
    bad = await drill.run(app, "attest", what="backup-offbox", evidence="too short")
    assert bad["ok"] is False and drill_log.latest_results(j) == {}
    ev = "restored shortbot-20261004T031700Z.db.gz from s3 on the laptop; integrity ok, 41 tables, trade 212"
    res = await drill.run(app, "attest", what="backup-offbox", evidence=ev)
    assert res["verdict"] == "pass" and res["attested"] and res["checks"]["attested"]["detail"] == ev
    st = await drill.run(app, "status")
    assert set(st["results"]) == {"backup-offbox"} and "untagged_max_s" in st["uptime"]
    assert drill_log.result_age_days(st["results"]["backup-offbox"]) < 0.01


def test_latest_result_wins_per_drill():
    j = Journal(":memory:")
    drill_log.result(j, "restart", "fail", {"x": drill_log.check(False)})
    drill_log.result(j, "restart", "pass", {"x": drill_log.check(True)})
    drill_log.result(j, "backup", "pass", {"x": drill_log.check(True)})
    got = drill_log.latest_results(j)
    assert {k: v["verdict"] for k, v in got.items()} == {"restart": "pass", "backup": "pass"}
    assert all(v["decision_id"] and v["ts"] and v["mode"] == "demo" for v in got.values())


def test_the_cli_declaration():
    a = build_parser().parse_args(["drill", "restart", "--phase", "after", "--wait-min", "5"])
    assert (a.name, a.phase, a.wait_min, a.symbol, a.second) == ("restart", "after", 5.0, "XRPUSDT", "ADAUSDT")
    a = build_parser().parse_args(["drill", "attest", "backup-offbox", "--evidence", "x"])
    assert (a.name, a.what, a.evidence) == ("attest", "backup-offbox", "x")
    from shortbot import commands
    cmd = commands.by_cli("drill")
    assert not cmd.write and not cmd.orders and cmd.name is None   # an orchestrator: its steps are the intents

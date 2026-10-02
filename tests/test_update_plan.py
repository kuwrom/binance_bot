"""update_plan on a trade whose plan carries the desk's pins (r1_usdt, full_size, ladder_live: adds, the live ladder,
split entries): the analyst edits the plan fields, the pins survive untouched and can't be edited."""

from types import SimpleNamespace

import pytest

from shortbot import mcp_trade
from shortbot.plans import revise_plan
from tests.test_entry_amend import desk, one
from tests.test_killswitch import prop


async def split_trade():
    d, t, fake, _ = desk()
    await one(d, prop(None, tranches=[{"limit_price": 1.03, "share": 0.5}, {"limit_price": 1.06, "share": 0.5}]))
    fake.fill_limit(next(i for i, o in fake.limits.items() if o["price"] == 1.03))
    (ev,) = await d.poll()
    tr = t.j.get_trade(ev["trade_id"])
    assert tr["plan"]["ladder_live"] == "v1" and tr["plan"]["r1_usdt"] and tr["plan"]["full_size"]
    return t, tr


async def test_update_plan_keeps_a_split_entry_trades_pins(monkeypatch):
    t, tr = await split_trade()
    monkeypatch.setattr(mcp_trade, "get_app", lambda: SimpleNamespace(journal=t.j))
    r = await mcp_trade.update_plan(tr["symbol"], {"time_stop_hours": 30, "auto": {"time_stop": True}}, "longer hold")
    assert r["ok"], r
    plan = t.j.get_trade(tr["id"])["plan"]
    assert plan["time_stop_hours"] == 30 and plan["auto"]["time_stop"] is True
    assert {k: plan[k] for k in ("ladder_live", "r1_usdt", "full_size")} == {
        k: tr["plan"][k] for k in ("ladder_live", "r1_usdt", "full_size")}


async def test_the_pins_are_not_editable(monkeypatch):
    t, tr = await split_trade()
    monkeypatch.setattr(mcp_trade, "get_app", lambda: SimpleNamespace(journal=t.j))
    r = await mcp_trade.update_plan(tr["symbol"], {"r1_usdt": 999}, "bigger 1R")
    assert not r["ok"] and "pinned" in r["plan_errors"][0]
    assert t.j.get_trade(tr["id"])["plan"]["r1_usdt"] == tr["plan"]["r1_usdt"]


def test_revise_plan_still_rejects_typos():
    with pytest.raises(ValueError, match="unknown plan fields"):
        revise_plan({"r1_usdt": 5.0}, {"time_stop_hourz": 3})
    plan, pins = revise_plan({"r1_usdt": 5.0, "time_stop_hours": 12}, {"trail_pct": 2, "trail_activate_pct": 3})
    assert plan.time_stop_hours == 12 and plan.trail_pct == 2 and pins == {"r1_usdt": 5.0}

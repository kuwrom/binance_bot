"""A plan's resting targets go on at the entry, under its symbol lock, right after the stop (#71), not at the guardian's
next pass up to 60 s later. A failed sync never fails the entry: the guardian's pass retries it."""

from shortbot import targets
from tests.fakes import SYM, FakeCcxt
from tests.test_target_orders import PLAN, guardian, resting, target_creates


async def opened(tmp_path, plan=PLAN):
    fake = FakeCcxt()
    g = guardian(fake, tmp_path / "shortbot.db")
    r = await g.app.trader.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100, plan=plan)
    return g, fake, r


async def favorable():
    return "favorable"


async def boom(*a, **k):
    raise RuntimeError("listing failed")


async def test_open_short_rests_the_targets_at_once_and_the_guardian_adds_none(tmp_path):
    g, fake, r = await opened(tmp_path)
    assert r["ok"] and r["resting_targets"] == {"cancelled": 0, "placed": 2}, r
    assert [(p, q) for p, q, _ in resting(fake)] == [(0.95, 50), (0.9, 50)]          # before any guardian pass
    assert all(c.startswith(f"sb-t{r['trade_id']}-") for _, _, c in resting(fake))
    assert [x["status"] for x in targets.rows(g.j)] == ["resting", "resting"]
    assert list(fake.stops.values()) == [1.05]
    kinds = [o[0] for o in fake.orders]
    assert kinds.index("STOP_MARKET") < kinds.index("limit")                          # the stop first
    n = len(fake.orders)
    await g.check_positions()
    await g.check_positions()
    assert len(fake.orders) == n and len(target_creates(fake)) == 2                   # no duplicates
    assert [(p, q) for p, q, _ in resting(fake)] == [(0.95, 50), (0.9, 50)]


async def test_a_plan_whose_targets_only_alert_lists_nothing_at_entry(tmp_path):
    g, fake, r = await opened(tmp_path, {**PLAN, "auto": {"targets": False}})
    assert r["ok"] and "resting_targets" not in r and target_creates(fake) == []


async def test_a_sync_failure_still_returns_ok_with_the_stop_placed(tmp_path, monkeypatch):
    monkeypatch.setattr("shortbot.targets_sync.listed", boom)
    g, fake, r = await opened(tmp_path)
    assert r["ok"] and r["amount"] == 100 and list(fake.stops.values()) == [1.05], r
    assert "error" in r["resting_targets"] and resting(fake) == []
    assert g.j.active_trade(SYM)["status"] == "open"
    assert "target_sync_error" in [a["kind"] for a in g.j.alerts(50)]
    monkeypatch.undo()
    await g.check_positions()                                                       # the guardian's pass retries
    assert [(p, q) for p, q, _ in resting(fake)] == [(0.95, 50), (0.9, 50)]


async def test_a_sync_failure_whose_alert_fails_too_still_returns_ok(tmp_path, monkeypatch):
    monkeypatch.setattr("shortbot.targets_sync.listed", boom)
    fake = FakeCcxt()
    g = guardian(fake, tmp_path / "shortbot.db")

    async def alert(kind, msg, symbol=None, urgent=False):
        if kind == "target_sync_error":
            raise ConnectionError("telegram down")
    g.app.trader._alert = alert
    r = await g.app.trader.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100, plan=PLAN)
    assert r["ok"] and list(fake.stops.values()) == [1.05] and "error" in r["resting_targets"], r


async def test_a_limit_fill_rests_its_targets_after_its_stop_and_the_guardian_adds_none(tmp_path):
    fake = FakeCcxt()
    g = guardian(fake, tmp_path / "shortbot.db")
    p = {"symbol": SYM, "limit_price": 1.04, "stop_price": 1.10, "setup": "pump_fade", "thesis": "sweep",
         "level_type": "prior_high", "level_reason": "1.05 high", "plan": PLAN}
    g.entries._regime_fn = favorable
    assert (await g.entries.propose_group([p]))["ok"]
    fake.fill_limit(next(i for i, o in fake.limits.items() if o["side"] == "sell"))
    ev = await g.entries.poll()
    filled = next(e for e in ev if e["event"] == "filled")
    qty = g.j.get_trade(filled["trade_id"])["amount"]
    assert list(fake.stops.values()) == [1.10] and fake.prearms == {}
    assert [(p, q) for p, q, _ in resting(fake)] == [(0.95, qty / 2), (0.9, qty / 2)]   # before any guardian pass
    assert all(c.startswith(f"sb-t{filled['trade_id']}-") for _, _, c in resting(fake))
    n = len(fake.orders)
    await g.check_positions()
    assert len(fake.orders) == n and len(target_creates(fake)) == 2

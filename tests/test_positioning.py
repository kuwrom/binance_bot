from datetime import datetime, timedelta, timezone

from shortbot.journal import Journal
from shortbot.positioning import PositionOps, chandelier_stop, reassess
from shortbot.trading import Trader
from tests.fakes import SYM, FakeCcxt, FakeEx, neutral_budget, ok_prechecks

NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


def candles(n=60, px=1.0, rng=0.02):
    return [[i, px, px * (1 + rng), px * (1 - rng), px, 1] for i in range(n)]


def trade(**kw):
    t = {"symbol": SYM, "entry_price": 1.0, "initial_stop": 1.05, "amount": 100, "mfe_pct": 0,
         "opened_at": (NOW - timedelta(hours=2)).isoformat(), "plan": {"time_stop_hours": 24}, "targets_hit": []}
    t.update(kw)
    return t


def kinds(r):
    return [a["action"] for a in r["actions"]]


def test_reassess_hold_and_R_math():
    r = reassess(trade(), {"mark": 1.0, "contracts": 100, "exchange_stop": 1.05}, candles(), 0.0001, 5.0, NOW)
    assert r["initial_R_usdt"] == 5.0 and r["risk_to_stop_R"] == 1.0
    assert "exit_or_shorten" not in kinds(r) and "reduce" not in kinds(r)


def test_reassess_flags_not_working_negative_funding_and_reduce():
    t = trade(opened_at=(NOW - timedelta(hours=13)).isoformat())
    r = reassess(t, {"mark": 1.0, "contracts": 100, "exchange_stop": 1.05}, candles(), -0.01, 2.0, NOW)
    ks = kinds(r)
    assert "exit_or_shorten" in ks and "shorten" in ks and "reduce" in ks


def test_reassess_suggests_widen_when_stop_inside_noise_and_chandelier():
    r = reassess(trade(), {"mark": 1.0, "contracts": 100, "exchange_stop": 1.01}, candles(rng=0.05), None, 5.0, NOW)
    assert "consider_widen_with_cut" in kinds(r)
    assert chandelier_stop(candles(rng=0.01)) > 0.99


async def ops_with_open_trade(**kw):
    fake = FakeCcxt(**kw)
    t = Trader(FakeEx(fake), Journal(":memory:"), prechecks=ok_prechecks, budget=neutral_budget)
    r = await t.open_short(SYM, stop_price=1.05, setup="pump_fade", thesis="t", notional=100)
    assert r["ok"]
    return PositionOps(t), t, fake


async def test_widen_cuts_size_to_keep_1R_and_only_once():
    ops, t, fake = await ops_with_open_trade()
    r = await ops.widen_stop(SYM, 1.10, "stop inside noise")
    # 1R = 0.05*100 = 5 USDT; at 1.10 allowed = 5/0.10 = 50 contracts
    assert r["ok"] and fake.position <= 50 and list(fake.stops.values()) == [1.10]
    assert (fake.position * (1.10 - 1.0)) <= 5.0 + 1e-9
    assert not (await ops.widen_stop(SYM, 1.12, "again"))["ok"]


async def test_add_only_to_winners_and_within_1R():
    ops, t, fake = await ops_with_open_trade()
    assert not (await ops.add_to_winner(SYM, 50, 1.02, "x"))["ok"]      # not in profit
    fake.price = 0.9
    bad = await ops.add_to_winner(SYM, 100, 1.04, "too loose")
    assert not bad["ok"] and "1R" in bad["error"]
    ok = await ops.add_to_winner(SYM, 50, 0.95, "trend continuing")
    assert ok["ok"] and fake.position > 100 and list(fake.stops.values()) == [0.95]


async def test_add_pins_1R_and_records_peak_size_so_later_adds_cannot_grow_the_budget():
    ops, t, fake = await ops_with_open_trade()
    tr = t.j.active_trade(SYM)
    r1 = (tr["initial_stop"] - tr["entry_price"]) * tr["amount"]
    fake.price = 0.9
    assert (await ops.add_to_winner(SYM, 50, 0.95, "first add"))["ok"]
    tr2 = t.j.active_trade(SYM)
    assert tr2["plan"]["r1_usdt"] == r1 and tr2["plan"]["full_size"] == tr["amount"]
    assert tr2["amount"] > tr["amount"]                       # targets are shares of the peak size
    from shortbot.positioning import _r_initial
    assert _r_initial(tr2) == r1                              # a bigger amount does not buy a bigger 1R


def test_plan_add_keeps_total_loss_at_stop_within_1R_and_shrinks_or_skips():
    from shortbot.ladder_apply import plan_add
    # short 100 @ 1.00, now 0.90, stop 1.05, 1R = 5 USDT: adding 100 needs avg-based stop <= 0.95 + 5/200 = 0.975
    qty, stop = plan_add(1.0, 100, 0.9, 1.05, 100, 5.0, 0.5)
    assert qty == 100 and abs(stop - 0.975) < 1e-9
    assert ((stop - (1.0 * 100 + 0.9 * qty) / (100 + qty)) * (100 + qty)) <= 5.0 + 1e-9
    # 1R nearly used up: the required stop would hug price -> halve, then give up
    assert plan_add(1.0, 100, 0.999, 1.05, 100, 0.05, 0.5) is None


def test_live_exposure_neutral_is_full_size_and_sweet_scores_scale_to_the_cap():
    from shortbot.ladder import decide, live_exposure_target, BASE
    assert live_exposure_target(0, 2.0) == 1.0 and live_exposure_target(2, 2.0) == 2.0
    assert live_exposure_target(-2, 2.0) == 0.0 and live_exposure_target(2, 1.0) == 1.0
    from datetime import datetime, timezone
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    hist = [{"ts": "2026-09-28T00:00:00+00:00", "x_star": 2.0, "x_virtual": 1.0, "action": "hold"}]
    act, x = decide(hist, 1.0, 2.0, now, {**BASE, "x_max": 2.0})
    assert act == "add" and 1.0 < x <= 2.0


async def test_add_beyond_1R_only_when_the_risk_param_allows_it():
    from dataclasses import replace
    ops, t, fake = await ops_with_open_trade()
    fake.price = 0.9
    # 1R = 5 USDT (100 @ 1.00, stop 1.05). Adding 100 with stop 1.00 -> avg 0.95, loss at stop 0.05*200 = 10 = 2R
    assert not (await ops.add_to_winner(SYM, 90, 1.0, "double"))["ok"]            # default cap 1R
    t.ex.settings = replace(t.ex.settings, limits=replace(t.ex.settings.limits, max_add_r_mult=2.0))
    r = await ops.add_to_winner(SYM, 90, 1.0, "double on a sweet score")
    assert r["ok"] and r["worst_case_usdt"] <= 10.0 + 1e-6
    from shortbot.positioning import _r_initial
    assert abs(_r_initial(t.j.active_trade(SYM)) - 5.0) < 1e-9                    # 1R stays pinned


async def test_add_rejected_while_a_split_leg_rests_and_no_needless_stop_move():
    ops, t, fake = await ops_with_open_trade()
    fake.price = 0.9
    t.j.db.execute("CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY, status TEXT, tranche TEXT, symbol TEXT,"
                   " order_id TEXT, reason TEXT)")
    t.j.db.execute("INSERT INTO entries (status, tranche, symbol, order_id) VALUES ('pending','tx',?,'1')", (SYM,))
    r = await ops.add_to_winner(SYM, 10, 1.04, "x")
    assert not r["ok"] and "leg" in r["error"]
    t.j.db.execute("DELETE FROM entries")
    assert (await t.move_stop(SYM, 1.0, "breakeven"))["ok"]
    before = list(fake.stops.values())
    ok = await ops.add_to_winner(SYM, 10, 1.0, "stop already fits the budget")    # no tightening needed
    assert ok["ok"] and list(fake.stops.values()) == before


def test_r_multiple_uses_the_pinned_1R_after_an_add():
    from shortbot.journal import Journal
    j = Journal(":memory:")
    tid = j.create_trade(SYM, "pump_fade", "t", 1.05, 3, {"r1_usdt": 5.0}, {}, None)
    j.activate_trade(tid, 1.0, 200, 200)                            # amount grew to 200 after an add
    j.close_trade(tid, 1.05, -10.0, "stop")
    assert j.closed_results()[-1]["r_multiple"] == -2.0              # a 2R loss, not the -1R the old math said

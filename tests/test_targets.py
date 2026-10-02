"""Resting plan targets (#65): the book a trade's automatic targets should have on the exchange (targets.desired)."""

from decimal import ROUND_DOWN, Decimal

from shortbot import clientid, targets

PLAN = {"targets": [{"price": 0.95, "fraction": 0.3}, {"price": 0.9, "fraction": 0.3}, {"price": 0.8, "fraction": 0.4}],
        "auto": {"targets": True}}


def floor(step=0.1):
    return lambda x: float((Decimal(str(x)) / Decimal(str(step))).to_integral_value(ROUND_DOWN) * Decimal(str(step)))


def trade(amount=100.0, hit=(), **plan):
    return {"id": 7, "amount": amount, "targets_hit": list(hit), "plan": {**PLAN, **plan}}


def book(t, contracts=100.0, mark=1.0, step=0.1):
    return [(w["target"], w["qty"]) for w in targets.desired(t, {"mark": mark, "contracts": contracts}, floor(step))]


def test_each_target_rests_its_own_share_nearest_first_and_the_last_takes_the_rest():
    assert book(trade()) == [(0.95, 30.0), (0.9, 30.0), (0.8, 40.0)]
    assert sum(q for _, q in book(trade(), contracts=99.9)) == 99.9          # never more than the position


def test_shares_are_of_the_original_size_and_manual_partials_count():
    assert book(trade(), contracts=80) == [(0.95, 10.0), (0.9, 30.0), (0.8, 40.0)]   # 20 already gone by hand
    assert book(trade(), contracts=50) == [(0.9, 10.0), (0.8, 40.0)]                 # T1's share is gone already
    assert book(trade(hit=[0.95]), contracts=70) == [(0.9, 30.0), (0.8, 40.0)]


def test_an_add_grows_the_shares():
    assert book(trade(amount=150), contracts=150) == [(0.95, 45.0), (0.9, 45.0), (0.8, 60.0)]


def test_crossed_hit_and_alert_only_targets_do_not_rest():
    assert book(trade(), mark=0.92) == [(0.9, 30.0), (0.8, 40.0)]   # 0.95 is crossed: the market fallback owns it
    assert book(trade(hit=[0.95, 0.9]), contracts=40) == [(0.8, 40.0)]
    assert book(trade(auto={"targets": False})) == []
    assert book(trade(targets=[])) == []
    assert book(trade(), contracts=0) == []


def test_shares_floor_to_the_step_and_differences_stay_exact():
    t = trade(targets=[{"price": 0.95, "fraction": 0.1}, {"price": 0.9, "fraction": 0.2}], amount=3.0)
    assert book(t, contracts=3.0) == [(0.95, 0.3), (0.9, 0.6)]                    # 0.3 + 0.6, not 0.8999...
    t = trade(targets=[{"price": 0.95, "fraction": 0.01}, {"price": 0.9, "fraction": 0.5}], amount=5.0)
    assert book(t, contracts=5.0) == [(0.9, 2.5)]                                  # a share below one step: none


def test_at_most_max_resting_targets():
    many = [{"price": 0.99 - i * 0.01, "fraction": 0.05} for i in range(15)]
    assert len(book(trade(targets=many))) == targets.MAX_RESTING


def test_target_client_ids_carry_the_trade():
    cid = clientid.new("target", 1234)
    assert cid.startswith("sb-t1234-") and targets.is_target(cid) and targets.trade_ref(cid) == 1234
    assert not targets.is_target("sb-e1-aa") and not targets.is_target(None) and targets.trade_ref("sb-tx-1") is None

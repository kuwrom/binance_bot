"""#64 (docs/PROPOSALS.md P1c): margin facts of every open short, checked in the protective positions pass after its
stops: isolated, a USDT/USDC margin asset, isolated margin >= 2x maintenance, and a resting stop inside liquidation
(tightened there, as #17's restore does). Multi-Assets mode is an account check on the monitor lane."""

import pytest

from shortbot import guardian, margin
from shortbot.alerts_view import is_urgent
from tests.fakes import SYM, FakeCcxt
from tests.test_guardian import guardian_with, kinds, open_trade

NEW_KINDS = ("adl_risk_persistent", "margin_not_isolated", "margin_asset", "margin_thin", "multi_assets_mode",
             "margin_check_error", "plan_stop_unreachable")


def rows(g, kind):
    return [a for a in g.j.alerts(500) if a["kind"] == kind]


def patch_info(fake, **fields):
    """The raw v3 row with some fields changed (None drops one): shapes the fake doesn't model."""
    real = fake.fetch_positions

    async def fetch():
        out = await real()
        for p in out:
            p["info"].update(fields)
            for k in [k for k, v in fields.items() if v is None]:
                del p["info"][k]
        return out
    fake.fetch_positions = fetch


def test_the_new_protective_kinds_are_urgent_in_the_brief():
    assert all(is_urgent(k, SYM) for k in NEW_KINDS)


async def test_a_healthy_isolated_short_raises_nothing():
    g = guardian_with(FakeCcxt())
    await open_trade(g)
    await g.check_positions()
    assert not [k for k in kinds(g) if k.startswith(("margin", "plan_stop"))]


async def test_a_crossed_position_is_an_urgent_alert_once_per_cooldown():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    fake.sym[SYM]["margin"] = "CROSSED"
    for _ in range(3):
        await g.check_positions()
    (a,) = rows(g, "margin_not_isolated")
    assert a["symbol"] == SYM and "isolated" in a["message"] and fake.stops   # alert-only: the stop stays


async def test_a_non_usdt_margin_asset_is_alerted():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    patch_info(fake, marginAsset="BNFCR")
    await g.check_positions()
    assert "BNFCR" in rows(g, "margin_asset")[0]["message"]


async def test_thin_isolated_margin_is_alerted():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    patch_info(fake, isolatedMargin="3.0", maintMargin="2.0")   # 1.5x maintenance
    await g.check_positions()
    assert "1.5x" in rows(g, "margin_thin")[0]["message"]


@pytest.mark.parametrize("fields", [{"maintMargin": None}, {"isolatedMargin": "n/a"}, {"marginAsset": None}])
async def test_unreadable_margin_fields_are_an_error_never_silence(fields):
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)
    patch_info(fake, **fields)
    await g.check_positions()
    assert rows(g, "margin_check_error")


async def test_a_margin_check_crash_never_costs_the_stop_restore(monkeypatch):
    fake = FakeCcxt()
    g = guardian_with(fake)
    await open_trade(g)

    def boom(p):
        raise RuntimeError("bug")
    monkeypatch.setattr(margin, "position_findings", boom)
    fake.stops.clear()
    await g.check_positions()
    assert list(fake.stops.values()) == [1.05] and "bug" in rows(g, "margin_check_error")[0]["message"]


# ------------------------------------------------------------ a resting stop past liquidation
async def drained(wallet_adj=-42.0):
    """2x short of 100 at 1.0, stop 1.05: the isolated wallet drained from 50 to 8 puts liquidation at 108/101 = 1.0693,
    so the stop sits past liquidation minus 2% (1.0479)."""
    fake = FakeCcxt()
    g = guardian_with(fake)
    tid = await open_trade(g)
    fake.sym[SYM]["wallet_adj"] = wallet_adj
    return fake, g, tid


def stop_orders(fake):
    return [o for o in fake.orders if (o[3] or {}).get("closePosition")]


async def test_a_stop_past_liquidation_is_tightened_inside_it_once_and_the_plan_stop_stays():
    fake, g, tid = await drained()
    cap = fake.liq_price(SYM) * 0.98
    await g.check_positions()
    (stop,) = fake.stops.values()
    assert cap - 0.0001 < stop <= cap and g.j.get_trade(tid)["stop_price"] == 1.05
    (a,) = rows(g, "plan_stop_unreachable")
    assert a["symbol"] == SYM and "1.05" in a["message"]
    placed = len(stop_orders(fake))
    for _ in range(3):   # at the cap now: no move, no alert
        await g.check_positions()
    assert len(stop_orders(fake)) == placed and len(rows(g, "plan_stop_unreachable")) == 1


async def test_a_drifting_liquidation_never_loosens_or_churns_the_stop():
    fake, g, _ = await drained()
    await g.check_positions()
    (first,) = fake.stops.values()
    placed = len(stop_orders(fake))
    fake.sym[SYM]["wallet_adj"] = -30.0          # margin added: liquidation moves away, the stop is never loosened
    await g.check_positions()
    fake.sym[SYM]["wallet_adj"] = -42.001        # liquidation 0.00001 lower: under a tick, no move
    await g.check_positions()
    assert list(fake.stops.values()) == [first] and len(stop_orders(fake)) == placed
    fake.sym[SYM]["wallet_adj"] = -42.5          # funding drained more: one tick or more, tightened again
    await g.check_positions()
    (second,) = fake.stops.values()
    assert second < first and second <= fake.liq_price(SYM) * 0.98
    assert len(rows(g, "plan_stop_unreachable")) == 1   # one alert per cooldown, not per move
    await g.check_positions()
    assert list(fake.stops.values()) == [second]


async def test_price_past_the_cap_closes_the_short_like_the_restore_does():
    fake, g, tid = await drained()
    fake.price = 1.049   # past liquidation minus 2% (1.0479), below the stop (1.05) and liquidation
    await g.check_positions()
    assert fake.position == 0 and g.j.get_trade(tid)["status"] == "closed"
    assert rows(g, "plan_stop_unreachable")


# ------------------------------------------------------------ Multi-Assets mode (monitor lane)
async def test_multi_assets_mode_is_an_urgent_alert():
    fake = FakeCcxt()
    g = guardian_with(fake)
    await g.check_margin_mode()
    assert not kinds(g)
    fake.multi_assets = True
    await g.check_margin_mode()
    assert "Multi-Assets" in rows(g, "multi_assets_mode")[0]["message"]
    assert "margin_mode" in guardian.MONITOR


@pytest.mark.parametrize("answer, on", [({"multiAssetsMargin": "true"}, True), ({"multiAssetsMargin": False}, False)])
async def test_the_multi_assets_answer_is_read_tolerantly(answer, on):
    assert margin.multi_assets(answer) is on


@pytest.mark.parametrize("answer", [{}, {"multiAssetsMargin": "maybe"}, [], None])
async def test_an_unexpected_multi_assets_answer_fails_the_job(answer):
    fake = FakeCcxt()
    g = guardian_with(fake)

    async def odd(req=None):
        return answer
    fake.fapiPrivateGetMultiAssetsMargin = odd
    with pytest.raises(ValueError, match="multiAssetsMargin"):
        await g.check_margin_mode()

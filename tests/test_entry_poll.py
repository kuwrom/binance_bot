"""The 5 s entry poll stays up when single entries can't be read (#8): one failing entry never stops the others, a
streak alerts once, a pass in which every entry failed raises (an outage: the guardian lane escalates), an order
Binance has forgotten past its GTD retires its row, and a hung ticker only skips the price rules."""

import asyncio

import ccxt
import pytest

from tests.test_entries_cancel import recorder
from tests.test_entry_races import SYM2, desk, prop


def fail_fetch(fake, oid):
    """Reading this order times out, every time (its conditional orders still answer)."""
    real = fake.fetch_order

    async def fetch(i, s, params=None):
        if i == oid and not (params or {}).get("trigger"):
            raise ccxt.RequestTimeout("binanceusdm GET https://demo-fapi.binance.com/fapi/v1/order timed out")
        return await real(i, s, params=params)
    fake.fetch_order = fetch


@pytest.mark.parametrize("split", [False, True])
async def test_an_entry_whose_order_cant_be_read_never_stops_the_others_being_polled(split):
    d, t, fake = desk()
    sent = recorder(t)
    legs = [{"limit_price": 1.04}, {"limit_price": 1.07}]
    stuck = [x["entry_id"] for x in (await d.propose_group([prop(1.04, tranches=legs) if split else prop(1.04)]))
             ["placed"]]
    other = (await d.propose_group([prop(2.08, stop=2.2, symbol=SYM2)]))["placed"][0]["entry_id"]
    for oid in [i for i, o in fake.limits.items() if o["price"] in (1.04, 1.07)]:
        fail_fetch(fake, oid)
    fake.set_price(SYM2, 2.09)
    assert [(x["entry_id"], x["event"]) for x in await d.poll()] == [(other, "filled")]
    assert sorted(x["id"] for x in d.pending()) == sorted(stuck)
    for _ in range(3):                                           # only the stuck ones are left: every one fails
        with pytest.raises(ccxt.RequestTimeout):
            await d.poll()
    failing = [(m, u) for k, m, s, u in sent if k == "entry_poll_failing"]
    assert len(failing) == len(stuck) and all(u and "RequestTimeout" in m for m, u in failing)   # once per streak


async def test_a_pass_in_which_every_entry_fails_raises_so_the_guardian_escalates():
    d, t, fake = desk()
    await d.propose_group([prop(1.04)])
    await d.propose_group([prop(2.08, stop=2.2, symbol=SYM2)])
    for oid in list(fake.limits):
        fail_fetch(fake, oid)
    with pytest.raises(ccxt.RequestTimeout):
        await d.poll()
    assert len(d.pending()) == 2


async def test_an_order_binance_forgot_after_its_gtd_retires_the_entry_and_its_prearmed_stop():
    d, t, fake = desk()
    eid = (await d.propose_group([prop(1.04)], ttl_hours=1))["placed"][0]["entry_id"]
    fake.advance(3 * 86400 + 3700)                               # the guardian was down: GTD lapsed, then forgotten
    with pytest.raises(ccxt.OrderNotFound):
        await d.poll()                                           # not past its TTL by the desk's clock: kept, raised
    d._update(eid, expires_ms=0)
    assert [x["event"] for x in await d.poll()] == ["expired"]
    assert d.pending() == [] and fake.prearms == {}
    assert "forgot" in d._rows("id=?", (eid,))[0]["reason"]


async def test_a_hung_ticker_is_bounded_and_skips_only_the_price_rules(monkeypatch):
    monkeypatch.setattr("shortbot.entries.TICKER_TIMEOUT_S", 0.05)
    d, t, fake = desk()
    await d.propose_group([prop(1.04, cancel_below=0.95)])
    other = (await d.propose_group([prop(2.08, stop=2.2, symbol=SYM2)]))["placed"][0]["entry_id"]

    async def hang(s):
        await asyncio.Event().wait()
    fake.fetch_ticker = hang                                     # both clients read through it
    fake.set_price(SYM2, 2.09)
    ev = await asyncio.wait_for(d.poll(), 2)
    assert [(x["entry_id"], x["event"]) for x in ev] == [(other, "filled")] and len(d.pending()) == 1


async def test_cancel_rules_read_the_book_the_order_rests_on():
    d, t, fake = desk()
    await d.propose_group([prop(1.04, cancel_below=0.95)])
    fake.prod.price = 0.94                                       # prod fell through cancel_below, the demo book didn't
    assert await d.poll() == [] and len(d.pending()) == 1
    fake.price = 0.94
    assert [x["event"] for x in await d.poll()] == ["cancelled"]

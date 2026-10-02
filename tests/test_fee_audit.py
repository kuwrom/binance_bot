"""fee_audit (#75): journalled fees and P&L of closed trades against Binance income matched by tradeId, read only. A
trade reconciled before #75 lost its entry COMMISSION (stamped to the second before the fill, probe_result 395)."""

import pytest

from shortbot import fee_audit, reconcile
from tests.test_probe_folds import pin
from tests.test_reconcile import SYM2, net, setup, short


async def closed_trade(t, fake):
    """A short stopped out on the exchange and reconciled, then journalled as the old code had it (no entry commission)."""
    pin(fake, 875)
    tid = await short(t)
    fake.advance(60)
    fake.price = 1.05
    await reconcile.reconcile(t)
    entry_fee = fake.fills[0]["fee"]["cost"]
    tr = t.j.get_trade(tid)
    t.j.update_trade(tid, fees=tr["fees"] + entry_fee, pnl=tr["pnl"] + entry_fee)
    return tid, entry_fee


async def test_the_audit_reports_the_missing_entry_commission_and_writes_nothing(monkeypatch):
    t, fake = setup(monkeypatch)
    tid, entry_fee = await closed_trade(t, fake)
    old = t.j.get_trade(tid)
    writes = t.j.db.total_changes
    r = await fee_audit.audit(t)
    assert t.j.db.total_changes == writes and t.j.get_trade(tid) == old
    (row,) = r["trades"]
    assert row["id"] == tid and row["before"]["fees"] == pytest.approx(old["fees"])
    assert row["after"]["net"] == pytest.approx(net(fake))
    assert row["diff"]["fees"] == pytest.approx(-entry_fee) and row["diff"]["net"] == pytest.approx(-entry_fee)
    assert row["diff"]["gross"] == pytest.approx(0) and row["diff"]["funding"] == pytest.approx(0)
    assert r["totals"]["net"] == pytest.approx(-entry_fee) and (r["audited"], r["skipped"]) == (1, 0)


async def test_a_trade_without_its_position_in_trade_history_is_skipped_with_the_reason(monkeypatch):
    t, fake = setup(monkeypatch)
    tid, _ = await closed_trade(t, fake)
    ghost = t.j.create_trade(SYM2, "pump_fade", "t", 2.2, 3)
    t.j.close_trade(ghost, 2.0, -1.0, "manual")
    open_ = await short(t, stop=1.10)                            # open trades are not audited
    r = await fee_audit.audit(t)
    assert [x["id"] for x in r["trades"]] == [ghost, tid] and (r["audited"], r["skipped"]) == (1, 1)
    assert "no_entry" in r["trades"][0]["skipped"] and open_ not in [x["id"] for x in r["trades"]]

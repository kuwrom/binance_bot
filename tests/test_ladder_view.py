"""Setup-ladder visibility (#67): governor_status carries a `ladders` map of the setups that are not NORMAL, the brief
says so in one line, and both stay cheap (only setups with trades in their epoch or a recorded stop; a 1 h cache that
a closed trade or a ladder write invalidates at once)."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from shortbot import risk_budget
from shortbot import setup_ladder_view as V
from shortbot.cli_brief import brief
from shortbot.journal import Journal
from tests.test_brief import app
from tests.test_setup_ladder import rows

SHADOW_PF = rows([2] + [-1] * 13)                 # pump_fade at its stop (prior 8.6 / 12.3)
NORMAL_FH = rows([1, 1], "funding_hi", 5)


def closed(*parts):
    """Journal.closed_results rows (the keys stats() and the governor read) for these ladder rows."""
    rs = [{**r, "id": k, "symbol": "QNT/USDT:USDT", "experiment": None, "conviction": "B", "regime_cell": None,
           "pnl": r["r_multiple"] * 10, "risk_usdt": 10.0, "limits": None, "session": None, "mae_pct": None,
           "mfe_pct": None} for k, r in enumerate(r for p in parts for r in p)]
    return lambda all_modes=False: rs


@pytest.fixture(autouse=True)
def fresh_cache():
    V._cache.clear()


async def governor(j):
    inp = risk_budget.BudgetInputs(1000.0, 1000.0, gov={"halt": False, "m_gov": 1.0, "reasons": [],
                                                       "daily": {"room_usd": 30.0}})

    async def gather(trader, regime, **kw):
        return inp
    orig, risk_budget.gather_inputs = risk_budget.gather_inputs, gather
    try:
        return await risk_budget.governor_status(SimpleNamespace(j=j), "favorable")
    finally:
        risk_budget.gather_inputs = orig


async def test_governor_status_maps_only_the_setups_that_are_not_normal():
    j = Journal(":memory:")
    j.closed_results = closed(SHADOW_PF, NORMAL_FH)
    g = await governor(j)
    assert set(g["ladders"]) == {"pump_fade"}
    pf = g["ladders"]["pump_fade"]
    assert pf["state"] == "SHADOW" and pf["mult"] == 0.0 and pf["dd_r"] == 13.0 and "SHADOW" in pf["reasons"][0]
    j.closed_results = closed(NORMAL_FH)
    held = (await governor(j))["ladders"]["pump_fade"]               # the recorded stop holds without the trades
    assert held["state"] == "SHADOW" and held["mult"] == 0.0
    assert (await governor(Journal(":memory:")))["ladders"] == {}


async def test_governor_status_reports_a_ladder_failure_instead_of_failing():
    stub = SimpleNamespace(closed_results=lambda all_modes=False: [])   # no kv, no db: the map can't be read
    g = await governor(stub)
    assert g["ladders"] is None and g["ladders_error"] and g["equity"] == 1000.0


def test_the_map_is_cached_until_a_trade_closes_or_a_ladder_row_changes(monkeypatch):
    j = Journal(":memory:")
    j.closed_results = closed(SHADOW_PF, NORMAL_FH)
    calls = []
    real = risk_budget._ladder
    monkeypatch.setattr(risk_budget, "_ladder", lambda jj, s: calls.append(s) or real(jj, s))
    first = V.ladders(j)
    n = len(calls)
    assert sorted(calls) == ["funding_hi", "pump_fade"] and V.ladders(j) == first and len(calls) == n
    j.closed_results = closed(SHADOW_PF, NORMAL_FH, rows([-1] * 9, "funding_hi", 6))   # funding_hi DD 9 >= 8.2
    assert V.ladders(j)["funding_hi"]["state"] == "HALF" and len(calls) > n
    n = len(calls)
    j.set("setup_ladder:pump_fade", j.get("setup_ladder:pump_fade").replace('"reenables": []',
                                                                             '"reenables": ["2026-10-01T13:31"]'))
    assert V.ladders(j)["pump_fade"]["state"] == "HALF" and len(calls) > n


def test_setups_without_trades_in_their_epoch_are_not_replayed(monkeypatch):
    j = Journal(":memory:")
    j.closed_results = closed(SHADOW_PF, NORMAL_FH)
    V.ladders(j)                                                   # records pump_fade's stop
    from shortbot import setup_ladder as L
    L.reset_epoch(j, "funding_hi", "human", "rule change", now=datetime(2026, 11, 1, tzinfo=UTC))
    V._cache.clear()
    calls = []
    monkeypatch.setattr(risk_budget, "_ladder", lambda jj, s: calls.append(s) or {"state": "NORMAL", "mult": 1.0})
    V.ladders(j)
    assert calls == ["pump_fade"]                                  # funding_hi's trades predate its epoch


def test_an_unknown_ladder_is_shown(monkeypatch):
    j = Journal(":memory:")
    j.closed_results = closed(NORMAL_FH)
    monkeypatch.setattr(risk_budget, "_ladder", lambda jj, s: risk_budget._ladder_fallback(jj, s, RuntimeError("x")))
    lad = V.ladders(j)
    assert lad["funding_hi"]["state"] == "UNKNOWN" and lad["funding_hi"]["mult"] == 0.5
    assert "funding_hi UNKNOWN x0.5" in V.brief_line(j)


async def test_the_brief_has_one_ladder_line_only_when_a_setup_is_not_normal():
    a = app()
    a.journal.closed_results = closed(NORMAL_FH)
    assert "setup ladder" not in await brief(a)
    a.journal.closed_results = closed(SHADOW_PF, NORMAL_FH, rows([-1] * 9, "funding_hi", 6))
    text = await brief(a)
    line = [ln for ln in text.splitlines() if "setup ladder" in ln]
    assert len(line) == 1 and "funding_hi HALF x0.5 (DD 9.0R)" in line[0]
    assert "pump_fade SHADOW x0 (DD 13.0R)" in line[0]
    assert "shortbot-admin ladder-status" in line[0]


async def test_a_broken_ladder_read_is_a_warning_line_not_a_failed_brief(monkeypatch):
    a = app()

    def boom(j):
        raise RuntimeError("db locked")
    monkeypatch.setattr(V, "ladders", boom)
    assert "⚠️ setup ladder check failed: RuntimeError: db locked" in await brief(a)

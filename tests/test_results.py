"""Typed intent results (#36 gateway step 3, GATEWAY.md §4.5): every result shape the writes return today maps to one
status, exact by what the intent sent, and round-trips through the intent's row unchanged (the body is what MCP, the
CLI and the web console get); a raised error is typed (errors.wrap) and round-trips through the row's error column."""

import asyncio

import ccxt
import pytest

from shortbot import clientid, errors, intents
from shortbot.commands import CommandError
from shortbot.intents import Intent, Ledger
from shortbot.journal import Journal
from shortbot.results import Result
from shortbot.symlock import AccountBusy, SymbolBusy
from tests import fake_errors as err
from tests.fakes import SYM, FakeCcxt
CID = "sb-cQNT-1f2e3d4c"


def led(*outcomes: str | None, sent: bool | None = None) -> Ledger:
    """A run that sent these client-id orders (None: in doubt), or only id-less writes (sent=True: a cancel)."""
    out = Ledger(sent=bool(outcomes) or bool(sent))
    out.orders = {f"sb-c{i}-0": o for i, o in enumerate(outcomes)}
    return out


NONE, CANCEL, ACKED, REJECTED, PART, DOUBT = led(), led(sent=True), led("filled"), led("rejected"), led("part_filled"), \
    led(None)

# (what returns it, the body, what the run sent, its status): every write's result shapes today
SHAPES = [
    ("open_short", {"ok": True, "trade_id": 7, "amount": 100.0, "fill": 1.0, "warnings": []}, ACKED, "done"),
    ("open_short: execution guard", {"ok": False, "wait": "spread 2%"}, NONE, "refused"),
    ("open_short: vetoed", {"ok": False, "vetoed": ["hostile"], "hint": "veto_override"}, NONE, "refused"),
    ("open_short: budget", {"ok": False, "rejected_by_budget": ["no room"], "budget": {}}, NONE, "refused"),
    ("open_short: risk", {"ok": False, "rejected_by_risk": ["cap"], "request": {}}, NONE, "refused"),
    ("open_short: plan", {"ok": False, "plan_errors": ["target above entry"]}, NONE, "refused"),
    ("open_short: beside an order", {"ok": False, "resting_entries": [{"id": 1}], "error": "x"}, NONE, "refused"),
    ("open_short: kill switch", {"ok": False, "kill_switch": "HALTED", "error": "halted"}, NONE, "refused"),
    ("open_short: entry refused", {"ok": False, "error": "entry order refused, no position: -2019"}, REJECTED,
     "failed"),
    ("open_short: stop failed, closed", {"ok": False, "error": "stop failed; position closed", "closed": {"pnl": -1}},
     led("filled", "rejected", "filled"), "failed"),
    ("open_short: naked", {"ok": False, "error": "UNPROTECTED POSITION", "trade_id": 7, "unprotected": True},
     led("filled", "rejected", "rejected"), "partial"),
    ("open_short: unknown", {"ok": False, "unknown": True, "trade_id": 7, "client_id": CID, "error": "x"}, DOUBT,
     "unknown"),
    ("open_short: filled short", {"ok": True, "trade_id": 7, "amount": 50.0}, led("part_filled", "placed"), "partial"),
    ("close_position", {"ok": True, "symbol": SYM, "closed_amount": 100.0, "exit": 0.9, "result": {}}, ACKED, "done"),
    ("close_position: partial", {"ok": True, "symbol": SYM, "closed_amount": 50.0, "remaining": 50.0}, ACKED, "done"),
    ("close_position: filled short", {"ok": True, "closed_amount": 25.0, "remaining": 75.0, "warning": "25 of 50"},
     PART, "partial"),
    ("close_position: incomplete", {"ok": False, "error": "50 still open", "closed_amount": 50.0, "remaining": 50.0},
     PART, "partial"),
    ("close_position: flat first", {"ok": True, "flat": True, "closed_amount": 0.0, "remaining": 0.0}, REJECTED,
     "done"),
    ("close_position: unknown", {"ok": False, "unknown": True, "client_id": CID, "error": "x"}, DOUBT, "unknown"),
    ("close_position: changed", {"ok": False, "position": 50.0, "error": "you expected 100"}, NONE, "refused"),
    ("close_position: noop", {"ok": False, "noop": True, "position": 50.0, "error": "nothing to close"}, NONE,
     "refused"),
    ("move_stop", {"ok": True, "symbol": SYM, "old_stop": 1.05, "new_stop": 1.0, "order_id": "9"},
     led("placed", "placed"), "done"),
    ("move_stop: noop", {"ok": False, "noop": True, "error": "stops only tighten"}, NONE, "refused"),
    ("move_stop: swap failed", {"ok": False, "error": "stop move failed; old stop 1.05 restored"},
     led("placed", "rejected", "placed"), "failed"),
    ("move_stop: no stop left", {"ok": False, "error": "restoring failed", "unprotected": True},
     led("placed", "rejected", "rejected"), "partial"),
    ("move_stop: crossed", {"ok": True, "closed": True, "reason": "price crossed the new stop"}, ACKED, "done"),
    ("widen_stop", {"ok": True, "old_stop": 1.05, "new_stop": 1.1, "size_cut": 10.0, "remaining": 90.0}, ACKED, "done"),
    ("widen_stop: risk", {"ok": False, "error": "x", "rejected_by_risk": ["x"], "room": {}}, NONE, "refused"),
    ("widen_stop: cut failed", {"ok": False, "error": "size cut failed; stop moved back", "size_cut": 0},
     led("placed", "rejected", "placed"), "failed"),
    ("add_to_winner", {"ok": True, "added": 10.0, "price": 0.9, "new_stop": 1.0}, ACKED, "done"),
    ("add_to_winner: unknown", {"ok": False, "unknown": True, "client_id": CID, "error": "x"}, DOUBT, "unknown"),
    ("add_to_winner: refused", {"ok": False, "error": "add refused: InsufficientMargin -2019"}, REJECTED, "failed"),
    ("propose_entries", {"ok": True, "group_id": "g1", "placed": [{"entry_id": 1}], "rejected": [], "unknown": []},
     led("placed"), "done"),
    ("propose_entries: one unknown", {"ok": True, "group_id": "g1", "placed": [], "rejected": [],
                                      "unknown": [{"entry_id": 3}]}, DOUBT, "unknown"),
    ("propose_entries: none placed", {"ok": False, "group_id": "g1", "placed": [], "rejected": [{"error": "x"}],
                                      "unknown": []}, REJECTED, "failed"),
    ("propose_entries: none sent", {"ok": False, "group_id": "g1", "placed": [], "rejected": [{"error": "veto"}],
                                    "unknown": []}, NONE, "refused"),
    ("amend_entry", {"ok": True, "entry_id": 1, "new_limit": 1.1, "risk_usdt": 2.0}, CANCEL, "done"),
    ("cancel_entry_group", {"ok": True, "cancelled": 2, "filled_first": [], "cancel_failed": []}, CANCEL, "done"),
    ("cancel_entry_group: some failed", {"ok": False, "cancelled": 1, "filled_first": [], "cancel_failed": [3]},
     CANCEL, "partial"),
    ("cancel_entry_group: none", {"ok": False, "cancelled": 0, "filled_first": [], "cancel_failed": [3]}, CANCEL,
     "failed"),
    ("flatten_all", {"ok": True, "state": "HALTED", "closed": [{"symbol": SYM}], "failed": [], "still_open": []},
     ACKED, "done"),
    ("flatten_all: some failed", {"ok": False, "state": "HALTED", "closed": [{"symbol": SYM}],
                                  "failed": [{"symbol": "X"}], "still_open": ["X"]}, ACKED, "partial"),
    ("flatten_all: none closed", {"ok": False, "state": "HALTED", "closed": [], "failed": [{"symbol": SYM}],
                                  "still_open": [SYM]}, REJECTED, "failed"),
    ("halt_trading", {"ok": True, "changed": True, "state": "HALTED", "resting": {"cancelled": 1}}, CANCEL, "done"),
    ("update_plan", {"ok": True, "trade_id": 7, "plan": {}}, NONE, "done"),
    ("add_learning (journal only)", {"ok": True, "learning_id": 3}, NONE, "done"),
    ("stale code", {"ok": False, "stale_code": True, "error": "nothing was sent"}, NONE, "refused"),
    ("busy", {"ok": False, "busy": True, "error": "another session is adding risk"}, NONE, "busy"),
    ("plan_step", {"ok": True, "trade_id": 7, "done": [{"kind": "close", "result": {"ok": True}}]}, ACKED, "done"),
    ("plan_step: its stop move failed", {"ok": False, "trade_id": 7, "done": [
        {"kind": "close", "result": {"ok": True}}, {"kind": "move_stop", "result": {"ok": False}}]},
     led("filled", "placed", "rejected", "placed"), "partial"),
    ("plan_step: flat", {"ok": True, "skipped": "flat"}, NONE, "done"),
    ("restore_stop: nothing due", None, NONE, "done"),
    ("restore_stop", {"symbol": SYM, "action": "stop_placed", "stop": 1.05}, led("placed"), "done"),
    ("cap_stop", {"symbol": SYM, "from": 1.2, "to": 1.1, "action": "failed", "error": "x"}, led("rejected"), "done"),
    ("force_close", {"ok": True, "symbol": SYM, "covered": False, "closed": True, "gone": False}, ACKED, "done"),
    ("pull_targets", True, CANCEL, "done"),
    ("cancel_entry", "unfilled", CANCEL, "done"),
    ("protect_fill", {"entry_id": 1, "event": "filled"}, led("placed"), "done"),
    ("withdraw_entries", {"unfilled": [1], "filled": [], "unknown": []}, CANCEL, "done"),
]


@pytest.fixture
def j(tmp_path):
    return Journal(tmp_path / "j.db")


def stored(j, r: Result) -> Result:
    """`r` through the journal: journalled, started, finished with it, read back."""
    it = Intent("t_act", {}, SYM)
    intents.receive(j, it)
    intents.start(j, it.id)
    assert intents.finish(j, it.id, r)
    return Result.from_row(intents.get(j, it.id))


@pytest.mark.parametrize("what, body, ran, status", SHAPES, ids=[s[0] for s in SHAPES])
def test_every_result_shape_maps_to_its_status_and_round_trips(j, what, body, ran, status):
    r = Result.of(body, ran)
    assert r.status == status and r.body is body and r.error is None and r.side == "short"
    assert r.sent is {"refused": False, "busy": False, "unknown": None}.get(status, ran.sent)
    assert stored(j, r) == r   # the body is today's result, unchanged: the wire stays as it is


@pytest.mark.parametrize("e, ran, status, kind", [
    (CommandError("no open journal trade"), NONE, "refused", errors.Unexpected),
    (SymbolBusy(SYM, 5, "held"), NONE, "busy", errors.Unexpected),
    (AccountBusy(5, "held"), NONE, "busy", errors.Unexpected),
    (RuntimeError("bug"), NONE, "refused", errors.Unexpected),
    (ccxt.RequestTimeout("binanceusdm GET positionRisk timed out"), NONE, "refused", errors.OutcomeUnknown),
    (err.margin_insufficient(), REJECTED, "failed", errors.InsufficientMargin),
    (RuntimeError("market close failed after 3 tries"), led("rejected", "rejected"), "failed", errors.Unexpected),
    (clientid.Unknown(CID, TimeoutError()), DOUBT, "unknown", errors.OutcomeUnknown),
    (RuntimeError("market close failed (outcome unknown)"), led("rejected", None), "unknown", errors.Unexpected),
    (RuntimeError("stop not placed"), led("filled", "rejected"), "partial", errors.Unexpected),
    (asyncio.CancelledError(), DOUBT, "interrupted", errors.Unexpected),
    (asyncio.CancelledError(), NONE, "interrupted", errors.Unexpected),
])
def test_a_raised_error_is_typed_and_its_status_exact_by_what_was_sent(j, e, ran, status, kind):
    r = Result.failure(e, ran)
    assert (r.status, errors.classify(r.error), r.body) == (status, kind, None)
    assert r.sent is ({"refused": False, "busy": False, "unknown": None, "interrupted": None if ran.sent else False}
                      .get(status, True))
    if not ran.sent:
        assert r.error.sent is False   # whatever its type: this run never reached Binance
    back = stored(j, r)
    assert (back.status, back.sent, type(back.error), back.error.to_dict()) == (
        r.status, r.sent, kind, r.error.to_dict())


def test_the_error_wire_form_names_the_type_the_code_and_what_was_sent():
    w = errors.wrap(err.margin_insufficient())
    assert w.to_dict() == {"type": "InsufficientMargin", "code": -2019, "message": str(err.margin_insufficient()),
                           "sent": True, "unknown": False, "retryable": False}
    assert errors.wrap(err.status_unknown()).sent is None and errors.wrap(err.too_many_requests()).sent is None
    assert errors.wrap(ValueError("x")).to_dict()["message"] == "ValueError: x"
    with pytest.raises(ccxt.InvalidOrder) as below_one_step:   # ccxt's own check: it never left
        FakeCcxt().amount_to_precision(SYM, 0.01)
    assert errors.wrap(below_one_step.value).sent is False
    for e in (err.reduce_only_rejected(), err.status_unknown(), err.one_close_position_stop(), ValueError("x")):
        w = errors.wrap(e)
        back = errors.OrderError.from_dict(w.to_dict())
        assert type(back) is type(w) and back.to_dict() == w.to_dict()
    gone = errors.OrderError(None, "never started")   # the sweep's expired intent: nothing ran
    assert errors.OrderError.from_dict(gone.to_dict()).to_dict() == gone.to_dict() and gone.sent is False
    assert errors.wrap(w) is w

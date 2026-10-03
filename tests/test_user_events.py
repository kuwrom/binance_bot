"""Which guardian job a user-data event wakes (#54): user_events.classify on the payloads demo pushed (probe_result
387) and the fake's stream (fake_stream.py)."""

import pytest

from shortbot import user_events as ue

# probe_result 387 (demo, 2026-10-02), a probe's limit and reduce-only stop, as received
LIMIT_NEW = {"e": "ORDER_TRADE_UPDATE", "T": 1790954054963, "E": 1790954054964, "o": {
    "s": "XRPUSDT", "c": "sb-x7XRP-14670487", "S": "SELL", "o": "LIMIT", "f": "GTC", "q": "3.5", "p": "1.5419",
    "ap": "0", "sp": "0", "x": "NEW", "X": "NEW", "i": 3513715662, "l": "0", "z": "0", "L": "0", "n": "0", "N": "USDT",
    "T": 1790954054963, "t": 0, "b": "0", "a": "5.39665", "m": False, "R": False, "wt": "CONTRACT_PRICE", "ot": "LIMIT",
    "ps": "BOTH", "cp": False, "rp": "0", "pP": False, "si": 0, "ss": 0, "V": "EXPIRE_MAKER", "pm": "NONE", "gtd": 0,
    "er": "0"}}
STOP_CANCELED = {"e": "ALGO_UPDATE", "T": 1790954062199, "E": 1790954062200, "o": {
    "caid": "sb-x7XRP-dee00752", "aid": 1000000226242931, "at": "CONDITIONAL", "o": "STOP_MARKET", "s": "XRPUSDT",
    "S": "BUY", "ps": "BOTH", "f": "GTC", "q": "3.5", "X": "CANCELED", "ai": "", "tp": "1.6629", "p": "0",
    "V": "EXPIRE_MAKER", "wt": "MARK_PRICE", "pm": "NONE", "cp": False, "pP": False, "R": True, "tt": 0, "gtd": 0,
    "ia": False}}


def order(cid, x, status, i=7):
    return {**LIMIT_NEW, "o": {**LIMIT_NEW["o"], "c": cid, "x": x, "X": status, "i": i}}


def algo(cid, status, ai=""):
    return {**STOP_CANCELED, "o": {**STOP_CANCELED["o"], "caid": cid, "X": status, "ai": ai}}


def test_the_demo_payloads_parse():
    h = ue.classify(LIMIT_NEW)
    assert (h.type, h.kind, h.job, h.symbol, h.cid, h.status, h.exec_type, h.event_ms, h.order_id) == (
        "ORDER_TRADE_UPDATE", "probe", None, "XRP/USDT:USDT", "sb-x7XRP-14670487", "NEW", "NEW", 1790954054964,
        "3513715662")
    h = ue.classify(STOP_CANCELED)
    assert (h.kind, h.job, h.status, h.order_id) == ("probe", None, "CANCELED", "1000000226242931")


@pytest.mark.parametrize("x,status,job", [
    ("NEW", "NEW", None),
    ("TRADE", "PARTIALLY_FILLED", None),   # a partial waits for the 5 s poll: adopting it sooner is a strategy change
    ("TRADE", "FILLED", "entries"),
    ("CANCELED", "CANCELED", "entries"),
    ("EXPIRED", "EXPIRED", "entries"),
    ("EXPIRED", "EXPIRED_IN_MATCH", "entries"),
])
def test_entry_events(x, status, job):
    h = ue.classify(order("sb-e12-1f2e3d4c", x, status))
    assert (h.kind, h.job) == ("entry", job)


@pytest.mark.parametrize("cid,x,status,kind,job", [
    ("sb-t41-0a0b0c0d", "TRADE", "PARTIALLY_FILLED", "target", "positions"),
    ("sb-t41-0a0b0c0d", "TRADE", "FILLED", "target", "positions"),
    ("sb-t41-0a0b0c0d", "NEW", "NEW", "target", None),
    ("sb-t41-0a0b0c0d", "CANCELED", "CANCELED", "target", None),
    ("autoclose-1790954054963", "TRADE", "FILLED", "liquidation", "positions"),
    ("adl_autoclose", "TRADE", "FILLED", "adl", "positions"),
    ("sb-c41-0a0b0c0d", "TRADE", "FILLED", "close", None),     # a session's close: it journals its own
    ("sb-o41-0a0b0c0d", "TRADE", "FILLED", "open", None),
    ("sb-a41-0a0b0c0d", "TRADE", "FILLED", "add", None),
    ("sb-x7XRP-14670487", "TRADE", "FILLED", "probe", None),
    ("web_8xUe2Ab", "TRADE", "FILLED", "foreign", None),        # not ours: logged, never acted on
    ("", "TRADE", "FILLED", "foreign", None),
])
def test_order_events(cid, x, status, kind, job):
    h = ue.classify(order(cid, x, status))
    assert (h.kind, h.job) == (kind, job)


@pytest.mark.parametrize("cid,kind", [("sb-s41-0a0b0c0d", "stop"), ("sb-p12-0a0b0c0d", "prearm"),
                                      ("sb-b41-0a0b0c0d", "bridge")])
@pytest.mark.parametrize("status,job", [("NEW", None), ("TRIGGERING", None), ("TRIGGERED", "positions"),
                                        ("FINISHED", "positions"), ("REJECTED", "positions"),
                                        ("EXPIRED", "positions"), ("CANCELED", "positions")])
def test_stop_events(cid, kind, status, job):
    """A cancelled whole-position stop may leave the position naked (cancelled in the app): the positions pass
    restores it at once. A pre-arm or a bridge is cancelled by the desk itself on every fill and stop swap: only its
    trigger, rejection or expiry matters."""
    h = ue.classify(algo(cid, status))
    assert (h.kind, h.job) == (kind, None if status == "CANCELED" and kind != "stop" else job)


def test_a_triggered_stop_names_the_order_it_fills_under_and_that_order_is_the_stop():
    h = ue.classify(algo("sb-s41-0a0b0c0d", "TRIGGERED", ai="88001"))
    assert h.actual_id == "88001" and h.kind == "stop"
    fill = order("autogen88001", "TRADE", "FILLED", i=88001)   # its own client id is undocumented
    assert ue.classify(fill).kind == "foreign"
    h = ue.classify(fill, {"88001": "stop"})
    assert (h.kind, h.job) == ("stop", "positions")


def test_other_events_are_counted_only():
    assert ue.classify({"e": "ACCOUNT_UPDATE", "E": 5, "a": {"m": "ORDER", "P": []}}).kind == "account"
    h = ue.classify({"e": "listenKeyExpired", "E": 9, "listenKey": "k"})
    assert (h.kind, h.job, h.event_ms) == ("expired", None, 9)
    for e in ({"e": "TRADE_LITE", "E": 1}, {"e": "MARGIN_CALL"}, {"no": "type"}, {"e": "ORDER_TRADE_UPDATE"}):
        assert ue.classify(e).job is None

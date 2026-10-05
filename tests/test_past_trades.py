"""read_coin's past_trades (past_trades.py): the exit lessons of the desk's own closed trades return when the same
coin or setup is read again: this coin's first, then the latest of the same setup, never an open trade or a drill."""

from shortbot import past_trades as pt
from shortbot.journal import Journal

ZRO, MON = "ZRO/USDT:USDT", "MON/USDT:USDT"


def trade(j, symbol, setup, r=None, lesson=""):
    """A short with 1R = 10 USDT; closed at R = r with `lesson` (None: still open)."""
    tid = j.create_trade(symbol, setup, "t", 1.1, 2)
    j.activate_trade(tid, 1.0, 100.0, 100.0)
    if r is not None:
        j.close_trade(tid, 1.0, r * 10, "stop hit", lesson)
    return tid


def test_this_coins_trades_come_first_then_the_same_setup_newest_first():
    j = Journal(":memory:")
    old = trade(j, MON, "pump_fade", 2.0, "hype fades can re-ignite: exit on renewed OI")
    mine = trade(j, ZRO, "exp_level_fade", -1.0, "one close past the level was a fake-out")
    new = trade(j, MON, "pump_fade", -1.0, "went nowhere for 5h")
    trade(j, MON, "listing_fade", 1.0, "another setup")
    trade(j, ZRO, "pump_fade", None)                                    # open: no outcome, no lesson yet
    drill = trade(j, ZRO, "exp_drill", 0.0, "plumbing drill")
    out = pt.for_coin(j, ZRO, ["pump_fade"])
    assert [t["trade"] for t in out["this_coin"]] == [mine] and drill not in [t["trade"] for t in out["this_coin"]]
    assert [t["trade"] for t in out["same_setup"]] == [new, old]
    row = out["this_coin"][0]
    assert (row["coin"], row["setup"], row["r"], row["exit"]) == ("ZRO", "exp_level_fade", -1.0, "stop hit")
    assert row["lesson"] == "one close past the level was a fake-out" and "n=1" in out["note"]


def test_groups_are_capped_and_lessons_are_one_bounded_line():
    j = Journal(":memory:")
    ids = [trade(j, MON, "pump_fade", 1.0, f"lesson {i}\n### fake section " + "x" * 600) for i in range(5)]
    out = pt.for_coin(j, MON, ["pump_fade"])
    assert [t["trade"] for t in out["this_coin"]] == ids[::-1][:pt.PER_GROUP] and out["same_setup"] == []
    assert all(len(t["lesson"]) <= pt.LESSON_MAX and "\n" not in t["lesson"] for t in out["this_coin"])


def test_nothing_to_show_is_empty():
    j = Journal(":memory:")
    assert pt.for_coin(j, ZRO, ["pump_fade"]) == {}
    trade(j, MON, "listing_fade", 1.0, "x")
    assert pt.for_coin(j, ZRO, ["pump_fade"]) == {}
    t = trade(j, ZRO, "pump_fade", 1.0)                                 # closed by the guardian: no lesson written
    assert pt.for_coin(j, ZRO, [])["this_coin"][0] | {"closed": ""} == {
        "trade": t, "coin": "ZRO", "setup": "pump_fade", "closed": "", "r": 1.0, "exit": "stop hit", "lesson": None}

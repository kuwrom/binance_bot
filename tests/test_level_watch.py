"""The between-scan level watch: a crossing counts once price is clearly past the level, never per tick on it."""

import pytest

from shortbot.level_watch import parse_levels, zone


def test_a_level_flips_the_zone_only_once_price_is_past_its_buffer():
    lv = [2.0, 2.045]
    assert zone(1.99, lv) == 0                       # first reading: no history, plain count
    assert zone(2.0004, lv, prev=0) == 0             # sitting on 2.000: no flip
    assert zone(2.007, lv, prev=0) == 1              # 0.35% past it: flipped
    assert zone(1.998, lv, prev=1) == 1              # back under by 0.1%: still the upper zone
    assert zone(1.99, lv, prev=1) == 0
    assert zone(2.06, lv, prev=0) == 2               # a jump across two levels


def test_levels_parse_into_exchange_ids():
    assert parse_levels(["zro=2.005,1.85", "GTC/USDT:USDT=0.2"]) == {"ZROUSDT": [1.85, 2.005], "GTCUSDT": [0.2]}
    with pytest.raises(ValueError, match="COIN=price"):
        parse_levels(["ZRO"])

"""Order-book depth feature (signals/depth.py): logged for research, no points or vetoes."""

from shortbot.signals import REGISTRY
from shortbot.signals.depth import depth_within


def test_depth_within_one_percent_per_side():
    ob = {"bids": [[100.0, 10], [99.5, 10], [98.0, 100]], "asks": [[100.2, 5], [100.9, 5], [102.0, 100]]}
    f = depth_within(ob)          # mid 100.1: bids >= 99.099, asks <= 101.101
    assert f["depth_bid_1pct_usd"] == round(100 * 10 + 99.5 * 10)
    assert f["depth_ask_1pct_usd"] == round(100.2 * 5 + 100.9 * 5)
    assert f["spread_bp"] == round(0.2 / 100.1 * 1e4, 2)


def test_empty_book_gives_none_not_an_error():
    assert depth_within({}) == {"depth_bid_1pct_usd": None, "depth_ask_1pct_usd": None, "spread_bp": None}


def test_depth_is_registered_research_only():
    spec = REGISTRY["depth"]
    assert spec.group == "tape" and spec.default_on

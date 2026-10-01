"""Guards for the trading.py split: the conftest patch of `shortbot.trading._sleep` must reach the mixed-in flows."""

import pytest

import shortbot.trading_manage
import shortbot.trading_open


@pytest.mark.parametrize("mod", [shortbot.trading_open, shortbot.trading_manage])
def test_flows_import_sleep_at_call_time(mod):
    # a module-level `_sleep` would be bound before the tests patch it: real waits in tests, silently
    assert not hasattr(mod, "_sleep"), f"{mod.__name__}: import _sleep inside the method, from .trading"

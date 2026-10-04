"""Enforcement (#36 step 7, write_guard.py): with the gateway on, an exchange write leaves a client process (an MCP
server, a CLI call, the web console, the guardian) only as break-glass or a probe, and leaves the gateway only from its
runs; the user-data stream's listen key always passes. A stray write raises before it is sent (WriteRefused: an
errors.Refused, nothing ran). On the real ccxt client (GuardedUSDM.fetch, HTTP faked below ccxt) and, through the lock
audit (conftest.py), on the fake every other test uses."""

from types import SimpleNamespace

import pytest

from shortbot import cmd_desk, errors, write_guard
from shortbot.config import RiskLimits, Settings
from shortbot.exchange import Exchanges
from shortbot.mcp_server import TOOLS
from tests.fake_http import FakeHost, FakeSession, wire
from tests.fakes import FakeCcxt
from tests.gateway_rig import as_session

ORDER = "https://demo-fapi.binance.com/fapi/v1/order"
KEY = "https://demo-fapi.binance.com/fapi/v1/listenKey"


@pytest.fixture
def role(monkeypatch):
    def set_(r):
        monkeypatch.setattr(write_guard, "_role", r)
    return set_


@pytest.mark.parametrize("kind, sessions, guardian, want", [
    ("gateway", False, False, "gateway"), ("mcp", True, True, "client"), ("cli", True, False, "client"),
    ("web", True, True, "client"), ("mcp", False, False, "local"), ("guardian", True, True, "client"),
    ("guardian", True, False, "local"), ("admin", True, True, "local"), (None, True, True, "local")])
def test_each_process_gets_its_role(monkeypatch, kind, sessions, guardian, want):
    monkeypatch.setattr(write_guard, "_role", "local")
    s = SimpleNamespace(gateway=sessions, gateway_guardian=guardian)
    assert write_guard.configure(kind, s) == want == write_guard.role()


def test_the_rule(role):
    role("client")
    assert write_guard.refusal("GET", ORDER) is None
    assert "outside the order gateway" in write_guard.refusal("POST", ORDER)
    for m in ("POST", "PUT", "DELETE"):
        assert write_guard.refusal(m, KEY) is None
    with write_guard.writing("breakglass"):
        assert write_guard.refusal("POST", ORDER) is None
    role("gateway")
    assert write_guard.refusal("DELETE", ORDER)   # the gateway writes from its runs only
    with write_guard.writing("gateway"):
        assert write_guard.refusal("DELETE", ORDER) is None
    role("local")
    assert write_guard.refusal("POST", ORDER) is None
    with pytest.raises(ValueError):
        with write_guard.writing("anything"):
            pass


@pytest.fixture
async def demo():
    host = FakeHost("demo-fapi.binance.com", 6000)
    ex = Exchanges(Settings("demo", "k", "s", ":memory:", "", "", RiskLimits()))
    wire(ex.trade, FakeSession(host, FakeHost("fapi.binance.com")))
    yield SimpleNamespace(t=ex.trade, host=host)
    await ex.close()


WRITES = [("fapiPrivatePostOrder", {"symbol": "XRPUSDT", "side": "SELL", "type": "MARKET", "quantity": 5}),
          ("fapiPrivatePostLeverage", {"symbol": "XRPUSDT", "leverage": 2}),            # set_leverage
          ("fapiPrivatePostMarginType", {"symbol": "XRPUSDT", "marginType": "ISOLATED"}),  # set_margin_mode
          ("fapiPrivatePostPositionSideDual", {"dualSidePosition": "false"}),           # set_position_mode
          ("fapiPrivateDeleteOrder", {"symbol": "XRPUSDT", "orderId": 1})]


@pytest.mark.parametrize("name, params", WRITES)
async def test_the_real_client_refuses_a_stray_write_before_sending_it(demo, role, name, params):
    role("client")
    with pytest.raises(write_guard.WriteRefused) as e:
        await getattr(demo.t, name)(params)
    assert errors.classify(e.value) is errors.Refused and e.value.sent is False
    assert demo.host.sent == []                                  # it never left the process
    with write_guard.writing("breakglass"):
        await getattr(demo.t, name)(params)
    assert len(demo.host.sent) == 1


async def test_reads_and_the_listen_key_pass(demo, role):
    role("client")
    await demo.t.fapiPrivateGetAccount()
    await demo.t.fapiPrivatePostListenKey()
    await demo.t.fapiPrivatePutListenKey()
    await demo.t.fapiPrivateDeleteListenKey()
    assert [m for m, _ in demo.host.sent] == ["GET", "POST", "PUT", "DELETE"]


async def test_a_stray_create_order_from_an_mcp_path_raises(monkeypatch, role, order_lock_audit):
    """A journal-only write command whose body reached the exchange (a regression): refused, nothing sent."""
    as_session(monkeypatch)
    role("client")
    fake = FakeCcxt()

    class Stray:
        def __init__(self, ex, j):
            pass

        async def start(self, symbols):
            return await fake.create_order("PUMP/USDT:USDT", "market", "sell", 5)
    monkeypatch.setattr("shortbot.warmup.WarmUp", Stray)
    monkeypatch.setattr(cmd_desk, "get_app", lambda: SimpleNamespace(ex=None, journal=_journal()))
    with pytest.raises(Exception, match="outside the order gateway"):
        await TOOLS["warmup_start"](None)
    assert fake.orders == []
    assert any("not declared orders=True" in b for b in order_lock_audit)   # the test-time audit saw it too
    order_lock_audit.clear()


def _journal():
    from shortbot.journal import Journal
    return Journal(":memory:")


# ------------------------------------------------------------ the documented exceptions, and the gateway's own runs
async def test_a_probe_writes_raw_in_a_client_process(role, monkeypatch):
    """probes.run opens its own context after its demo-only refusals (GATEWAY.md §1.2): raw cases still run."""
    from shortbot import probes
    from tests.test_probes import desk
    tr, fake = desk()
    role("client")
    r = await probes.run(tr, "close-stop-at-flat", "XRPUSDT")
    assert r["ok"] and fake.orders


async def test_the_guardian_as_a_client_writes_only_through_the_gateway_or_break_glass(role, monkeypatch):
    from tests.gateway_rig import served, short_dir
    from tests.test_gateway_guardian import acts, client_guardian
    from tests.test_guardian import guardian_with, open_trade
    fake = FakeCcxt()
    g = client_guardian(fake)
    await open_trade(g)
    role("client")
    async with served(g.j, **acts(g)):   # the gateway's run (its start-up, then the pass's restore) may write
        fake.stops.clear()
        await g.check_positions()
    assert list(fake.stops.values()) == [1.05]
    fake.stops.clear()
    monkeypatch.setattr("shortbot.gateway_wire.socket_path", lambda s: short_dir() / "gateway-demo.sock")
    g.gateway_link.ok()
    await g.check_positions()               # no gateway: break-glass may write
    assert list(fake.stops.values()) == [1.05]
    local = guardian_with(fake)             # a guardian writing in-process in the client role: refused
    fake.stops.clear()
    await local.check_positions()
    assert fake.stops == {} and "stop_restore_failed" in [a["kind"] for a in local.j.alerts()]

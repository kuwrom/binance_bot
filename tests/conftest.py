import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    async def instant(_s):
        return None
    monkeypatch.setattr("shortbot.trading._sleep", instant)


@pytest.fixture(autouse=True)
def warm_desk(request, monkeypatch):
    """Order-flow tests assume a warm desk; test_warmup.py exercises the real gate."""
    if "test_warmup" not in request.node.nodeid:
        monkeypatch.setattr("shortbot.warmup.gate", lambda j, now=None: None)


@pytest.fixture(autouse=True)
def guardian_alive(request, monkeypatch):
    """Order-flow tests assume a live guardian; test_killswitch*.py exercise the real heartbeat watchdog."""
    if "test_killswitch" not in request.node.nodeid:
        async def alive(tr, now=None):
            return None
        monkeypatch.setattr("shortbot.killswitch.watch_guardian", alive)


@pytest.fixture(autouse=True)
def current_code(request, monkeypatch):
    """Tool tests assume the MCP server runs the code on disk (editing files mid-run must not refuse them);
    test_stale_guard.py exercises the real check (#68)."""
    from shortbot import procs, stale_guard
    stale_guard.reset()
    if "test_stale_guard" not in request.node.nodeid:
        monkeypatch.setattr(stale_guard, "_disk", procs.code_version)
    yield
    stale_guard.reset()


_SRC = str(Path(__file__).resolve().parents[1] / "src" / "shortbot")
_TESTS = str(Path(__file__).resolve().parent)
# name: its symbol argument; None = an account-wide write, held under the account lock (#74)
_WRITES = {"create_order": 0, "cancel_order": 1, "edit_order": 1, "cancel_all_orders": 0,
           "set_margin_mode": 1, "set_leverage": 1, "set_position_mode": None}


def _adds_risk(name: str, a: tuple, k: dict) -> bool:
    """An order that opens or adds to a short: a SELL that isn't reduce-only (market entry, limit entry, add)."""
    params = (a[5] if len(a) > 5 else k.get("params")) or {}
    return name == "create_order" and a[2] == "sell" and not params.get("reduceOnly")


def _origin(frame) -> str | None:
    """The outermost shortbot function on the call chain that reached the exchange (test frames are skipped). None
    when a test drove a private primitive (`_place_stop`, `_market_close`...) directly: no sequence to lock."""
    outer = None
    while frame:
        f = frame.f_code.co_filename
        if f.startswith(_SRC):
            if not f.endswith(("/audit.py", "/symlock.py")):   # refusals / busy_refusal wrap the order paths
                outer = frame
        elif not f.startswith(_TESTS):
            break
        frame = frame.f_back
    if outer is None or outer.f_code.co_name.startswith("_") or outer.f_code.co_name == "withdraw":
        return None   # withdraw() is a primitive too: its callers hold the lock
    return f"{Path(outer.f_code.co_filename).stem}.{outer.f_code.co_name}"


@pytest.fixture(autouse=True)
def order_lock_audit(monkeypatch):
    """Task #21: every order-changing exchange call shortbot makes runs inside that symbol's lock, in every test. #74:
    every risk-adding send and account-wide write also runs inside the account lock (so the guardian, which never
    takes it, can never add risk). Probes are exempt: demo-only minimum orders that bypass the caps by design (#55)."""
    from shortbot import symlock
    from tests.fakes import FakeCcxt
    tables, bad = [], []
    real_init = symlock.SymbolLocks.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        tables.append(self)
    monkeypatch.setattr(symlock.SymbolLocks, "__init__", init)

    def audit(name, at):
        real = getattr(FakeCcxt, name)

        async def audited(self, *a, **k):
            origin = _origin(sys._getframe(1))
            what = f"{name}({a[at] if at is not None else ''}) via {origin}"
            if origin and at is not None and not any(t.holds(a[at]) for t in tables):
                bad.append(f"{what} outside its symbol lock")
            if (origin and (at is None or _adds_risk(name, a, k)) and not origin.startswith("probe")
                    and not any(t.holds_account() for t in tables)):
                bad.append(f"{what} outside the account lock")
            return await real(self, *a, **k)
        monkeypatch.setattr(FakeCcxt, name, audited)
    for name, at in _WRITES.items():
        audit(name, at)
    yield bad
    assert not bad, bad

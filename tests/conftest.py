import importlib
import os
import re
import sys
from pathlib import Path

import pytest

from tests import data_guard, net_guard

# Before anything imports shortbot.config, whose import loads the checkout's .env (the desk's keys and Telegram token):
# with this set it doesn't, here or in any process a test starts (#80).
os.environ["SHORTBOT_NO_DOTENV"] = "1"
assert "shortbot.config" not in sys.modules, "shortbot was imported before tests/conftest.py: .env may be loaded"

_GUARD = data_guard.SESSION
_NET = net_guard.SESSION
_BEFORE = pytest.StashKey[dict]()


def pytest_configure(config):
    config.addinivalue_line("markers", f"{net_guard.SPAWN_MARK}: the test starts a shortbot module or script in a "
                            "child process on purpose (tests/net_guard.py)")


def pytest_sessionstart(session):
    session.config.stash[_BEFORE] = data_guard.snapshot(data_guard.REPO_DATA)
    sys.addaudithook(_GUARD)
    sys.addaudithook(_NET)


def pytest_runtest_logstart(nodeid, location):
    _GUARD.test = _NET.test = nodeid


def pytest_sessionfinish(session, exitstatus):
    """Fail the run if it wrote under data/ (tests/data_guard.py) or reached the network outside a test
    (tests/net_guard.py: a background thread still running after its test ended)."""
    changed = data_guard.changes(session.config.stash[_BEFORE], data_guard.snapshot(data_guard.REPO_DATA))
    lines = [f"blocked: {s}" for s in _GUARD.seen + _NET.seen]   # _NET: attempts no test's teardown reported
    if changed and (os.getenv("CI") or _GUARD.seen):
        lines += [f"changed: {c}" for c in changed]
    elif changed:
        lines.append(f"note: data/ changed during the run, by no write of this process ({len(changed)} paths: the desk "
                     "running from this checkout?)")
    if lines:
        tr = session.config.pluginmanager.get_plugin("terminalreporter")
        if tr:
            red = bool(_GUARD.seen or _NET.seen or os.getenv("CI"))
            tr.write_sep("=", "data and network guards (#43, #80)", red=red)
            for line in lines:
                tr.write_line(line)
    if _GUARD.seen or _NET.seen or (changed and os.getenv("CI")):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def isolated_data(tmp_path_factory, monkeypatch):
    """No test touches the live desk (#43) or holds its secrets (#80). The journal (SHORTBOT_DB, hence
    provenance.data_dir() and every store beside it: research/*.db incl. long_shadow and trials, locks, backups) lives in
    a fresh temp dir, which is also the working directory, so a relative "data/..." default (streams.db, hist.db,
    daily.db, logs, charts) lands there too. .env is never loaded (SHORTBOT_NO_DOTENV, above); what the shell exported is
    dropped: every BINANCE_* / TELEGRAM_* key and token, LIVE_CONFIRM, and every SHORTBOT_* setting but the journal path
    and the flag. A test that needs one sets it itself. The app singleton starts empty: a keyed app one test built via
    get_app() never reaches the next."""
    root = tmp_path_factory.mktemp("desk")
    (root / "data").mkdir()
    for k in [k for k in os.environ if k.startswith(("BINANCE_", "TELEGRAM_", "SHORTBOT_")) or k == "LIVE_CONFIRM"]:
        if k != "SHORTBOT_NO_DOTENV":
            monkeypatch.delenv(k)
    monkeypatch.setenv("SHORTBOT_DB", str(root / "data" / "shortbot.db"))
    monkeypatch.setattr(importlib.import_module("shortbot.app"), "_app", None)
    monkeypatch.chdir(root)
    return root


@pytest.fixture(autouse=True)
def no_network(request):
    """No test reaches the network or starts a shortbot process (#80, tests/net_guard.py): an attempt fails the test
    that made it, even when the code under test swallowed the refusal."""
    n = len(_NET.seen)
    _NET.spawn_ok = request.node.get_closest_marker(net_guard.SPAWN_MARK) is not None
    yield
    _NET.spawn_ok = False
    hits = _NET.seen[n:]
    del _NET.seen[n:]   # reported here; the session summary keeps only attempts made outside a test
    if hits:
        pytest.fail("\n".join(hits), pytrace=False)


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
_RAW_WRITE = re.compile(r"^(fapi|sapi|dapi)?[Pp]rivate(Post|Put|Delete)")   # implicit endpoints that write


# Trader's send primitives (#36 step 0) and withdraw(): their callers hold the locks, so a test may drive them directly
PRIMITIVES = {"trading.prepare", "trading.place_close_stop", "trading_orders.close_market",
              "trading_orders.open_market", "trading_orders.cancel", "trading_orders.place_limit",
              "trading_orders.edit_limit", "trading_orders.place_reduce_stop", "trading_orders.place_reduce_limit",
              "entries_cancel.withdraw"}
# #36 step 2b: a write reached from the guardian (any guardian*.py frame) or from one of these runs inside an intent
INTENT_ONLY = {"entries.poll", "entry_intents.reconcile", "entry_intents.resolve", "entry_gates.sweep",
               "entry_gates.guardian_sweep", "entry_gates.halt_sweep", "orphans.sweep", "killswitch.watch_guardian",
               "entries_cancel.cancel_resting"}
# ...except these, by design (GATEWAY.md §1.2): the user-data stream's listen key changes no order or position, the
# stream must never wait for the journal, and step 9 moves the stream into the gateway (test_trader_public.NO_INTENT)
NO_INTENT = {"fapiPrivatePostListenKey": "listen_key", "fapiPrivatePutListenKey": "keep_listen_key"}


def _adds_risk(name: str, a: tuple, k: dict) -> bool:
    """An order that opens or adds to a short: a SELL that isn't reduce-only (market entry, limit entry, add)."""
    params = (a[5] if len(a) > 5 else k.get("params")) or {}
    return name == "create_order" and a[2] == "sell" and not params.get("reduceOnly")


def _origin(frame) -> str | None:
    """The outermost shortbot function on the call chain that reached the exchange (test frames are skipped). None
    when a test drove a primitive (PRIMITIVES) or a private helper directly: no sequence to lock."""
    outer = None
    while frame:
        f = frame.f_code.co_filename
        if f.startswith(_SRC):
            if not f.endswith(("/audit.py", "/symlock.py")):   # refusals / busy_refusal wrap the order paths
                outer = frame
        elif not f.startswith(_TESTS):
            break
        frame = frame.f_back
    if outer is None or outer.f_code.co_name.startswith("_"):
        return None
    origin = f"{Path(outer.f_code.co_filename).stem}.{outer.f_code.co_name}"
    return None if origin in PRIMITIVES else origin


def intent_root(frame) -> str | None:
    """The guardian-side function (INTENT_ONLY, or any in guardian*.py) on the call chain, if any: the whole chain,
    through asyncio's frames (wait_for awaits the coroutine in the caller's task)."""
    while frame:
        f = frame.f_code.co_filename
        if f.startswith(_SRC):
            name = f"{Path(f).stem}.{frame.f_code.co_name}"
            if name.startswith("guardian") or name in INTENT_ONLY:
                return name
        frame = frame.f_back
    return None


@pytest.fixture(autouse=True)
def order_lock_audit(monkeypatch):
    """Task #21: every order-changing exchange call shortbot makes runs inside that symbol's lock, in every test. #74:
    every risk-adding send and account-wide write also runs inside the account lock (so the guardian, which never
    takes it, can never add risk). Probes are exempt: demo-only minimum orders that bypass the caps by design (#55).
    #36 step 2b: every write the guardian makes (and the session watchdog's withdrawal) runs inside an intent
    (gateway.py), except the listen key (NO_INTENT)."""
    from shortbot import audit as audit_ctx
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
            if audit_ctx.intent_id() is None and (root := intent_root(sys._getframe(1))):
                bad.append(f"{what} outside an intent (from {root})")
            return await real(self, *a, **k)
        monkeypatch.setattr(FakeCcxt, name, audited)
    for name, at in _WRITES.items():
        audit(name, at)

    def raw(name):   # POST/PUT/DELETE endpoints (no symbol lock: account-wide or probes): the intent rule only
        real = getattr(FakeCcxt, name)

        async def audited(self, *a, **k):
            if name not in NO_INTENT and audit_ctx.intent_id() is None and (root := intent_root(sys._getframe(1))):
                bad.append(f"{name} outside an intent (from {root})")
            return await real(self, *a, **k)
        monkeypatch.setattr(FakeCcxt, name, audited)
    for name in [n for n in dir(FakeCcxt) if _RAW_WRITE.match(n)]:
        raw(name)
    yield bad
    assert not bad, bad


@pytest.fixture
def sent(monkeypatch):
    """Every order write the fake gets, with the intent running when it was sent (audit.intent_id): tests/intent_jobs."""
    from shortbot import audit as audit_ctx
    from tests.fakes import FakeCcxt
    out: list[tuple[str, str | None]] = []
    for name in ("create_order", "cancel_order", "edit_order"):
        real = getattr(FakeCcxt, name)

        async def spy(self, *a, _real=real, _name=name, **k):
            out.append((_name, audit_ctx.intent_id()))
            return await _real(self, *a, **k)
        monkeypatch.setattr(FakeCcxt, name, spy)
    return out


@pytest.fixture(autouse=True)
def no_user_stream_network(monkeypatch):
    """A guardian started in a test never reaches Binance's user-data stream: tests inject fake_stream.Hub.connect."""
    async def offline(url):
        raise ConnectionRefusedError("tests: no network")
    monkeypatch.setattr("shortbot.user_stream.open_socket", offline)

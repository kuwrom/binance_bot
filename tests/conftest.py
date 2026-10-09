import importlib
import os
import re
import sys
from contextvars import ContextVar
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
def ledger_busy_default(monkeypatch):
    """A Guardian built in a test makes the weight ledger fail fast (weight_ledger.fail_fast): only for that test."""
    from shortbot import weight_ledger
    monkeypatch.setattr(weight_ledger, "BUSY_MS", weight_ledger.BUSY_MS)


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
def no_cited_files(request, tmp_path, monkeypatch):
    """A brief built in a test checks no citations: the repo's DOCTRINE.md and skills cite the desk's learning ids,
    which mean nothing in a test journal. test_doctrine_check.py reads the real files and its own."""
    if "test_doctrine_check" not in request.node.nodeid:
        monkeypatch.setattr("shortbot.doctrine_check.DOCTRINE", tmp_path / "no-doctrine.md")
        monkeypatch.setattr("shortbot.doctrine_check.SKILLS", tmp_path / "no-skills")


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
# #36 step 7: the request each write is, for write_guard's rule (GuardedUSDM.fetch applies it to the real client)
_REQUEST = {"create_order": ("POST", "/fapi/v1/order"), "cancel_order": ("DELETE", "/fapi/v1/order"),
            "edit_order": ("PUT", "/fapi/v1/order"), "cancel_all_orders": ("DELETE", "/fapi/v1/allOpenOrders"),
            "set_margin_mode": ("POST", "/fapi/v1/marginType"), "set_leverage": ("POST", "/fapi/v1/leverage"),
            "set_position_mode": ("POST", "/fapi/v1/positionSide/dual")}


def _request(name: str) -> tuple[str, str]:
    """A raw implicit write's request: fapiPrivatePostListenKey -> POST /fapi/v1/listenKey."""
    if name in _REQUEST:
        return _REQUEST[name]
    m = re.match(r"^(?:fapi|sapi|dapi)?[Pp]rivate(Post|Put|Delete)(\w+)$", name)
    assert m, name
    return m.group(1).upper(), "/fapi/v1/" + m.group(2)[0].lower() + m.group(2)[1:]


def _write_guard(name: str) -> None:
    """The fake refuses what the real client would (write_guard.refusal), before the request."""
    from shortbot import write_guard
    method, path = _request(name)
    if why := write_guard.refusal(method, "https://demo-fapi.binance.com" + path):
        raise write_guard.WriteRefused(None, why)


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
    (gateway.py), except the listen key (NO_INTENT). #36 step 4: only a write command declared orders=True reaches the
    exchange (_orders_only)."""
    from shortbot import audit as audit_ctx
    from shortbot import symlock
    from tests.fakes import FakeCcxt
    tables, bad = [], []
    real_init = symlock.SymbolLocks.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        tables.append(self)
    monkeypatch.setattr(symlock.SymbolLocks, "__init__", init)
    not_orders = _orders_only(monkeypatch)

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
            if why := not_orders(what):
                bad.append(why)
            _write_guard(name)
            return await real(self, *a, **k)
        monkeypatch.setattr(FakeCcxt, name, audited)
    for name, at in _WRITES.items():
        audit(name, at)

    def raw(name):   # POST/PUT/DELETE endpoints (no symbol lock: account-wide or probes): the intent rule only
        real = getattr(FakeCcxt, name)

        async def audited(self, *a, **k):
            if name not in NO_INTENT and audit_ctx.intent_id() is None and (root := intent_root(sys._getframe(1))):
                bad.append(f"{name} outside an intent (from {root})")
            if why := not_orders(name):
                bad.append(why)
            _write_guard(name)
            return await real(self, *a, **k)
        monkeypatch.setattr(FakeCcxt, name, audited)
    for name in [n for n in dir(FakeCcxt) if _RAW_WRITE.match(n)]:
        raw(name)
    _size_events(monkeypatch, tables, bad)
    yield bad
    assert not bad, bad


_KIND: ContextVar[str | None] = ContextVar("test_intent_kind", default=None)


def _orders_only(monkeypatch):
    """With SHORTBOT_GATEWAY on, a session sends the write commands declared orders=True to the gateway and runs the
    rest itself: an exchange write inside any other write command's intent would bypass the gateway. Probes stay raw
    and local (GATEWAY.md §1.2). The kind comes from the LocalGateway.submit the write runs under."""
    from shortbot import commands, gateway
    real = gateway.LocalGateway.submit

    async def submit(self, intent, *a, **k):
        tok = _KIND.set(intent.kind)
        try:
            return await real(self, intent, *a, **k)
        finally:
            _KIND.reset(tok)
    monkeypatch.setattr(gateway.LocalGateway, "submit", submit)

    def check(what: str) -> str | None:
        cmd = commands.by_kind(_KIND.get() or "")
        if cmd is None or cmd.orders or cmd.kind == "probe":
            return None
        return f"{what} from {cmd.kind}: a write command not declared orders=True (it would bypass the gateway)"
    return check


def _size_events(monkeypatch, tables: list, bad: list[str]) -> None:
    """#82: a size change (close_intent.SIZE_EVENTS) is journalled in a unit its caller awaits (or a sync run), inside
    the symbol's lock, so the next reader under that lock, in any process, sees it (max_adds, close_intent). A queued
    unit runs in the journal's drain task, so the lock is checked as held by this process, not by the current task.
    A test seeding rows itself is exempt."""
    from shortbot.close_intent import SIZE_EVENTS
    from shortbot.journal import Journal
    from shortbot.symlock import _key
    real = Journal.log_decision

    def log_decision(self, action, reasoning, symbol=None, *a, **k):
        caller = sys._getframe(1).f_code
        if action in SIZE_EVENTS and not caller.co_filename.startswith(_TESTS):
            what = f"{action} decision from {Path(caller.co_filename).stem}.{caller.co_name}"
            if self.db.unit not in ("write", "run"):
                bad.append(f"{what} not in an awaited unit ({self.db.unit or 'posted'})")
            if not any(_key(symbol) in t._held for t in tables):
                bad.append(f"{what} outside its symbol lock")
        return real(self, action, reasoning, symbol, *a, **k)
    monkeypatch.setattr(Journal, "log_decision", log_decision)


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
def write_role(monkeypatch):
    """Every test starts in the local role (write_guard: in-process writes, as with the gateway off); a test that
    builds an app as an MCP server or the guardian with the flag on gets the client role it would have."""
    from shortbot import write_guard
    monkeypatch.setattr(write_guard, "_role", "local")


@pytest.fixture(autouse=True)
def no_user_stream_network(monkeypatch):
    """A guardian started in a test never reaches Binance's user-data stream: tests inject fake_stream.Hub.connect."""
    async def offline(url):
        raise ConnectionRefusedError("tests: no network")
    monkeypatch.setattr("shortbot.user_stream.open_socket", offline)


@pytest.fixture(autouse=True)
def fresh_sigma_state():
    """The probability layer's process caches start empty: closed-bar histories (vol_har, keyed by symbol and bar),
    the σ-scale and σ-model verdicts (recal, keyed by journal id, which a later journal can reuse) and the model
    sigma_hourly falls back on."""
    from shortbot.probability import recal, vol_har
    vol_har._HIST.clear()
    recal._cache.clear()
    recal._mcache.clear()
    recal._kv_model[0] = None
    recal._active["model"] = "ewma"
    yield

"""Trader's public interface (#36 gateway step 0, GATEWAY.md §8): no module outside the trading mixins (trading*.py)
reaches into a private Trader member, and none but the probes sends an exchange write through the raw client: every
order, cancel, edit and account write goes through a public Trader primitive. Tests may touch anything."""

import ast
import re
from pathlib import Path

from shortbot.trading import Trader
from tests import conftest
from tests.conftest import PRIMITIVES

SRC = Path(__file__).resolve().parents[1] / "src" / "shortbot"
WRITES = {"create_order", "cancel_order", "cancel_orders", "cancel_all_orders", "edit_order", "set_leverage",
          "set_margin_mode", "set_position_mode"}
RAW_WRITE = re.compile(r"^(fapi|sapi|dapi)?[Pp]rivate(Post|Put|Delete)")   # implicit endpoints: POST/PUT/DELETE
RAW_OK = ("probe*.py",)   # demo-only probes observe Binance's raw answers by design (#55, GATEWAY.md §8 step 0)
# inside the mixins, the primitives (close_market and open_market through their send loops) are all that send
SENDERS = {"prepare", "place_close_stop", "_close_sends", "_sell_once", "cancel", "place_limit", "edit_limit",
           "place_reduce_stop", "place_reduce_limit", "listen_key", "keep_listen_key"}
# #36 step 2b: the guardian sends through these outside an intent, by design (GATEWAY.md §1.2). The user-data stream's
# listen key changes no order or position, the stream must never wait for the journal (its REST calls are time-boxed),
# and step 9 moves the stream into the gateway; until then step 7's enforcement must allow it.
NO_INTENT = {"listen_key": "POST /fapi/v1/listenKey: the user-data stream's key, no order or position",
             "keep_listen_key": "PUT /fapi/v1/listenKey: its keepalive, no order or position"}
GUARDIAN = ("guardian*.py", "user_stream.py")   # the guardian's own modules (its shared flows: the runtime guard)


def mixins() -> list[Path]:
    return sorted(SRC.glob("trading*.py"))


def private_members() -> set[str]:
    """Private methods of the classes in trading*.py, and the private attributes they set on self."""
    names = set()
    for p in mixins():
        for c in (n for n in ast.parse(p.read_text()).body if isinstance(n, ast.ClassDef)):
            for n in ast.walk(c):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(n.name)
                elif (isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name)
                      and n.value.id == "self"):
                    names.add(n.attr)
    return {n for n in names if n.startswith("_") and not n.startswith("__")}


def private_uses(src: str, names: set[str]) -> list[tuple[int, str]]:
    """`x._name` (call, read or assignment; x not `self`: another class's own member), getattr/setattr(x, "_name")."""
    out = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Attribute) and n.attr in names and not (isinstance(n.value, ast.Name)
                                                                      and n.value.id in ("self", "cls")):
            out.append((n.lineno, n.attr))
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id in ("getattr", "setattr", "hasattr", "delattr") and len(n.args) > 1
              and isinstance(n.args[1], ast.Constant) and n.args[1].value in names):
            out.append((n.lineno, n.args[1].value))
    return out


def raw_writes(tree: ast.AST) -> list[tuple[int, str]]:
    """Calls of a ccxt write method: `<anything>.create_order(...)`, `.fapiPrivatePostX(...)`..."""
    return [(n.lineno, n.func.attr) for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and (n.func.attr in WRITES or RAW_WRITE.match(n.func.attr))]


def outside(skip: tuple[str, ...] = ()) -> list[Path]:
    return [p for p in sorted(SRC.rglob("*.py"))
            if p not in mixins() and not any(p.parent == SRC and p.match(s) for s in skip)]


def test_no_module_outside_the_trading_mixins_uses_a_private_trader_member():
    names = private_members()
    assert {"_alert", "_close_outcome", "_sell_once"} <= names   # the scan sees all four mixins
    bad = [f"{p.relative_to(SRC)}:{line}: Trader.{name}" for p in outside()
           for line, name in private_uses(p.read_text(), names)]
    assert not bad, "give Trader a public method instead:\n" + "\n".join(bad)


def test_exchange_writes_go_through_trader_primitives():
    bad = [f"{p.relative_to(SRC)}:{line}: .{name}()" for p in outside(RAW_OK)
           for line, name in raw_writes(ast.parse(p.read_text()))]
    assert not bad, "send it through a public Trader primitive (trading_orders.py):\n" + "\n".join(bad)


def test_inside_the_mixins_only_the_primitives_send():
    """So the gateway's step 8 moves the primitives alone (GATEWAY.md §7): no flow in trading*.py sends on its own."""
    bad = [f"{p.name}:{line}: {f.name} sends .{name}()" for p in mixins() for f in ast.walk(ast.parse(p.read_text()))
           if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name not in SENDERS
           for line, name in raw_writes(f)]
    assert not bad, "send it through a primitive:\n" + "\n".join(bad)
    assert {n for n in SENDERS if not n.startswith("_")} <= set(dir(Trader))


def test_the_guards_catch_each_shape():
    names = {"_place_stop", "_alert"}
    src = '''
async def f(tr, self):
    await tr._place_stop("X", 1.0)
    app.trader._alert = None
    getattr(tr, "_place_stop")
    self._place_stop()          # another class's own member
    await tr.place_close_stop("X", 1.0)
'''
    assert sorted(private_uses(src, names)) == [(3, "_place_stop"), (4, "_alert"), (5, "_place_stop")]
    src = '''
async def g(tr, t):
    await tr.t.create_order("X", "limit", "sell", 1, 1)
    await t.cancel_order("1", "X")
    await t.fapiPrivatePostCountdownCancelAll({})
    await t.fapiPrivateGetSymbolConfig({})
    await tr.cancel("X", "1")
'''
    assert sorted(raw_writes(ast.parse(src))) == [(3, "create_order"), (4, "cancel_order"),
                                       (5, "fapiPrivatePostCountdownCancelAll")]


def test_the_lock_audits_primitives_are_public_trader_methods():
    """conftest.PRIMITIVES (module.function): a test may drive these directly, as their callers hold the locks."""
    for origin in PRIMITIVES - {"entries_cancel.withdraw"}:
        stem, name = origin.split(".")
        assert not name.startswith("_") and getattr(Trader, name).__module__ == f"shortbot.{stem}", origin


def _handlers(tree: ast.AST) -> set[str]:
    """Functions registered as intent handlers: decorated @handler(...) (gateway.handler)."""
    return {f.name for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(isinstance(d, ast.Call) and getattr(d.func, "id", getattr(d.func, "attr", None)) == "handler"
                    for d in f.decorator_list)}


def primitive_calls(tree: ast.AST) -> list[tuple[str, int, str]]:
    """(enclosing function, line, primitive) for each call of a public sending primitive (task.cancel() has no
    arguments: not Trader.cancel)."""
    sends = {n for n in SENDERS | {p.split(".")[1] for p in PRIMITIVES} if not n.startswith("_")}
    return [(f.name, n.lineno, n.func.attr) for f in ast.walk(tree)
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(f)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in sends
            and (n.args or n.func.attr in NO_INTENT)]


def test_the_guardian_sends_only_from_intent_handlers_and_the_listen_key():
    """#36 step 2b: in the guardian's own modules a primitive is called only inside an intent handler, or is one the
    allowlist names (NO_INTENT, mirrored by the runtime guard in conftest). Its shared flows (entries, entry_intents,
    orphans, targets_sync, killswitch) are checked at run time: conftest.order_lock_audit."""
    bad, seen = [], set()
    for p in [p for g in GUARDIAN for p in sorted(SRC.glob(g))]:
        tree = ast.parse(p.read_text())
        inner = {(line, name) for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and f.name in _handlers(tree) for _, line, name in primitive_calls(f)}
        seen |= {(p.stem, name, (line, name) in inner) for _, line, name in primitive_calls(tree)}
        bad += [f"{p.name}:{line}: {fn} calls .{name}()" for fn, line, name in primitive_calls(tree)
                if (line, name) not in inner and name not in NO_INTENT]
    assert {("guardian_positions", "close_market", True), ("user_stream", "listen_key", False)} <= seen   # not vacuous
    assert not bad, "send it from an intent handler (gateway.handler):\n" + "\n".join(bad)
    assert set(NO_INTENT) <= SENDERS and set(conftest.NO_INTENT.values()) == set(NO_INTENT)

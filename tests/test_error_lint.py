"""No order-path module swallows an error silently (#37): every broad `except` (bare, Exception, BaseException) either
re-raises, tells a human, or states why carrying on is right in a `# noqa: BLE001 - <reason>` a reviewer can audit."""

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "shortbot"
SCOPE = ("trading*.py", "entries*.py", "entry_*.py", "stops.py", "positioning.py", "targets_sync.py",
         "close_intent.py", "killswitch_ops.py", "guardian*.py", "orphans.py")
# calls that tell a human (Trader.alert, Notifier.send, guardian_loop.send_safe, errors.report) or hand the error to
# the code that does (entries._poll_failed, guardian_positions._position_failed, guardian_loop._failed)
ALERTS = {"alert", "send", "send_safe", "notify", "report", "_poll_failed", "_position_failed", "_failed"}
REASON = re.compile(r"#\s*noqa:\s*BLE001\b.*?-\s*\w")   # a dash, then the reason
BROAD = {"Exception", "BaseException"}


def _broad(t: ast.expr | None) -> bool:
    names = [t] if t is None or not isinstance(t, ast.Tuple) else t.elts
    return any(n is None or (isinstance(n, ast.Name) and n.id in BROAD) for n in names)


def _handles(body: list[ast.stmt]) -> bool:
    for n in (x for st in body for x in ast.walk(st)):
        if isinstance(n, ast.Raise):
            return True
        if isinstance(n, ast.Call):
            f = n.func
            if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")) in ALERTS:
                return True
    return False


def violations(path: Path) -> list[str]:
    src = path.read_text()
    lines = src.splitlines()
    out = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.ExceptHandler) and _broad(n.type) and not _handles(n.body):
            if not REASON.search(lines[n.lineno - 1]):
                out.append(f"{path.name}:{n.lineno}: broad except that neither re-raises nor alerts, and gives no "
                           "`# noqa: BLE001 - <reason>`")
    return out


def scope() -> list[Path]:
    return sorted({p for pat in SCOPE for p in SRC.glob(pat)})


def test_order_path_never_swallows_an_error_without_a_stated_reason():
    assert len(scope()) >= 20
    bad = [v for p in scope() for v in violations(p)]
    assert not bad, "\n".join(bad)


def test_the_lint_catches_each_silent_shape(tmp_path):
    f = tmp_path / "trading_x.py"
    f.write_text('''
async def a(t, log):
    try:
        await t.cancel_order(1)
    except Exception:
        pass
    for i in range(2):
        try:
            await t.cancel_order(i)
        except Exception as e:
            log.warning("cancel %s: %s", i, e)
            continue
    try:
        await t.cancel_order(3)
    except:  # noqa: BLE001
        return None
    try:
        await t.cancel_order(4)
    except (ValueError, BaseException):  # noqa: BLE001 -
        x = 1
    try:
        await t.cancel_order(5)
    except Exception:  # noqa: BLE001 - the listing decides what still rests
        pass
    try:
        await t.cancel_order(6)
    except Exception as e:
        await t.alert("cancel_failed", str(e), None, False)
    try:
        await t.cancel_order(7)
    except Exception:
        raise
    try:
        await t.cancel_order(8)
    except ValueError:
        pass
''')
    got = violations(f)
    assert sorted(int(v.split(":")[1]) for v in got) == [5, 10, 15, 19], got

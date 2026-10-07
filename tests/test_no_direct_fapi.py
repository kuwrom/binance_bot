"""No code calls fapi.binance.com outside the weight guard (2026-10-07: a raw-urllib script got the IP banned 14 min).

The guarded paths: ccxt clients built by exchange.Exchanges (they take their URLs from ccxt, never from a literal) and
shortbot.http (scripts, lab, level_watch, lifecycle_data, the replay builders). Any other module holding a fapi URL in
code (a string literal or an f-string part, docstrings and comments aside) is a direct call in the making."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {"src/shortbot/http.py"}      # the helper itself: BASE and DEMO_BASE
HOSTS = ("fapi.binance.com",)           # demo-fapi.binance.com contains it too


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def fapi_literals(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(), str(path))
    docs = _docstrings(tree)
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
            and any(h in n.value for h in HOSTS)]


def sources() -> list[Path]:
    files = [*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").rglob("*.py"), *(ROOT / "deploy").rglob("*.py")]
    return sorted(p for p in files if "__pycache__" not in p.parts)


def test_only_the_guarded_helper_holds_a_fapi_url():
    hits = [f"{p.relative_to(ROOT)}:{line}: {text!r}" for p in sources() if str(p.relative_to(ROOT)) not in ALLOWED
            for line, text in fapi_literals(p)]
    assert not hits, "direct fapi URLs outside shortbot.http (use http.get / http.AsyncClient):\n" + "\n".join(hits)


def test_the_scan_sees_literals_and_f_strings_but_not_docstrings(tmp_path):
    f = tmp_path / "m.py"
    f.write_text('"""Calls fapi.binance.com, says the doc."""\n'
                 'URL = "https://fapi.binance.com/fapi/v1"\n'
                 'def g(p):\n    """fapi.binance.com again."""\n    return f"https://demo-fapi.binance.com{p}"\n')
    assert [line for line, _ in fapi_literals(f)] == [2, 5]

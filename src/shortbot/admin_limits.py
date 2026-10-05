"""`shortbot-admin [--mode live] limits status|set|unset`: the human's per-desk risk limits (desk_limits.py).

- `status`: every limit's effective value and source (default / .env / desk / research);
- `set KEY=VALUE ... --reason`: field or env-var names, any case; validated (desk_limits.coerce, then the combination);
  shows old -> new, asks for the code on the terminal like `clear`, swaps only the row the human saw, journals
  `desk_limits_changed` and alerts it;
- `unset KEY ... --reason`: drops back to .env / default, same confirmation.
This is the only writer of the desk-limits row: the MCP server and the `shortbot` CLI never write it, so the AI can't
raise a limit (tests/test_desk_limits_writers.py)."""

from __future__ import annotations

import argparse
from typing import Any

from . import desk_limits as dl
from . import killswitch as ks
from .config import Settings
from .journal import Journal

ACTIONS = ("status", "set", "unset")


def add_parser(sub: Any) -> None:
    lp = sub.add_parser("limits", help="this desk's risk limits (human-only; set/unset ask for a code on this terminal)")
    lp.add_argument("action", choices=ACTIONS)
    lp.add_argument("items", nargs="*", metavar="KEY[=VALUE]", help="set: KEY=VALUE pairs; unset: KEY names. KEY is a "
                    "RiskLimits field or its .env name (max_leverage or MAX_LEVERAGE)")
    lp.add_argument("--reason", default="", help="why (set / unset): journalled and alerted")


def _fmt(v: Any) -> str:
    return "unset" if v is None else f"{v:g}" if isinstance(v, (int, float)) else str(v)


async def run(args: argparse.Namespace, s: Settings, j: Journal) -> dict[str, Any]:
    from .admin import _by, _under_claude, confirm  # the same confirm-code path as `clear`
    if args.action == "status":
        from .params import ResearchParams
        d = dl.describe(s, j, ResearchParams(s, j))
        return {"ok": True, "mode": s.mode, "limits": {k: {"value": r["value"], "source": r["source"]}
                                                       for k, r in d["fields"].items()}, "desk": d["desk"]}
    if not (args.reason or "").strip() or _under_claude():
        return {"ok": False, "error": _under_claude() or f"limits {args.action} needs a --reason"}
    if not args.items:
        return {"ok": False, "error": f"limits {args.action}: name at least one limit"}
    changes: dict[str, str] = {}
    drop: tuple[str, ...] = ()
    errs: list[str] = []
    if args.action == "set":
        for item in args.items:
            k, eq, v = item.partition("=")
            if not eq or not k.strip() or not v.strip():
                errs.append(f"{item!r}: expected KEY=VALUE")
            else:
                changes[k] = v.strip()
    else:
        drop = tuple(args.items)
        errs += [f"{name!r}: unset takes names only" for name in drop if "=" in name]
    if errs:
        return {"ok": False, "error": "; ".join(errs)}
    k = dl.key(j)
    shown = j.get(k)   # the exact row the human confirms; the write swaps only that row
    cur, new, errs = dl.validate_changes(s, j, changes, drop)
    if errs:
        return {"ok": False, "error": "; ".join(errs)}
    diff = {f: {"from": cur.get(f), "to": new.get(f)} for f in sorted(set(cur) | set(new)) if cur.get(f) != new.get(f)}
    if not diff:
        return {"ok": False, "error": "nothing would change"}
    env = s.limits
    lines = "\n".join(f"  {f}: {_fmt(d['from'])} -> {_fmt(d['to'])}"
                      + (f" (back to .env/default {_fmt(getattr(env, f))})" if d["to"] is None else "")
                      for f, d in diff.items())
    why = confirm(f"[{s.mode.upper()}] desk limits, human-owned (the guardian and every check use them):\n{lines}\n"
                  f"apply to the {s.mode} desk")
    if why:
        return {"ok": False, "error": why}
    if not dl.write(j, new, shown):
        return {"ok": False, "error": "the desk limits changed while you were confirming: nothing changed, re-run"}
    by, note = _by(), args.reason.strip()
    j.log_decision("desk_limits_changed", f"desk limits ({s.mode}) changed by {by}: {note}", None,
                   snapshot={"mode": s.mode, "by": by, "reason": note, "diff": diff, "desk_limits": new})
    summary = ", ".join(f"{f} {_fmt(d['from'])} -> {_fmt(d['to'])}" for f, d in diff.items())
    await ks.notify(j, s, "desk_limits_changed", f"desk limits ({s.mode}) changed by {by}: {summary}. Reason: {note}",
                    urgent=False)
    return {"ok": True, "mode": s.mode, "changed": diff, "desk_limits": new, "by": by}

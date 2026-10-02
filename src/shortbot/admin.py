"""`shortbot-admin`: the HUMAN side of the kill switch (killswitch.py). Loosening the state lives in this separate
console script because the agent's allowlist, `Bash(.venv/bin/shortbot *)`, auto-approves every `shortbot`
subcommand, so a `shortbot` subcommand can't tell the human from the agent. Two loosenings live here:
- `clear`: lowers the state, and holds the loss trips at the readings it shows (killswitch_rearm.py) so they don't
  re-fire on the loss just reviewed;
- `equity-peak reset`: moves the drawdown peak to today's live equity (equity_marks.reset_peak).
Both swap only what the human confirmed: if it changed meanwhile, nothing changes and they re-run.

Speed bumps and a tripwire, not a security boundary (an agent with a shell can still write the SQLite file):
- a separate executable: the space before `*` is part of a Claude Code rule, so `.venv/bin/shortbot *` doesn't match
  `.venv/bin/shortbot-admin`; the owner also denies `Bash(*shortbot-admin*)` outright (deny holds in every mode);
- a real terminal: the confirmation code is read from /dev/tty, which Claude Code's Bash tool doesn't have, and
  it refuses to run when CLAUDECODE is set;
- every change is journalled (`kill_switch_clear`) and alerted, so a clear nobody typed gets noticed.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import secrets
from typing import Any

from . import equity_marks
from . import killswitch as ks
from . import killswitch_rearm as rearm
from .config import Settings
from .journal import Journal


def _tty():
    return open("/dev/tty", "r+", encoding="utf-8")   # OSError (ENXIO) without a controlling terminal


def _under_claude() -> str | None:
    return ("refused under Claude Code: run shortbot-admin in your own terminal" if os.environ.get("CLAUDECODE")
            else None)


def confirm(question: str) -> str | None:
    """None when a human at a terminal typed the code shown under `question`; else why not."""
    if _under_claude():
        return _under_claude()
    try:
        tty = _tty()
    except OSError as e:
        return f"needs an interactive terminal (/dev/tty: {e})"
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(4))
    with tty:
        tty.write(f"{question}? type {code} to confirm: ")
        tty.flush()
        typed = tty.readline().strip().upper()
    return None if typed == code else "confirmation code did not match: nothing changed"


def _by() -> str:
    return f"human:{getpass.getuser()} (tty)"


async def live_equity(settings: Settings) -> float:
    """Account equity (totalMarginBalance) read from the exchange now, as the guardian reads it."""
    from .app import close_app, get_app
    try:
        return float((await get_app(research=False).trader.account())["equity"])
    finally:
        await close_app()


async def run(args: argparse.Namespace, settings: Settings | None = None, journal: Journal | None = None,
              equity_fn=live_equity) -> dict[str, Any]:
    s = settings or Settings.from_env()
    j = journal or Journal(s.db_path, s.mode)
    if args.cmd == "status":
        return ks.status(j)
    if args.cmd == "equity-peak":
        return await reset_peak(j, s, args.reason, equity_fn)
    shown = j.get(ks.KV_KEY)   # the exact row the human confirms; the clear swaps only that row
    st = ks.read(j)
    if ks.LEVEL[args.to] >= st["level"]:
        return {"ok": False, "error": f"the desk is {st['state']}: {args.to} is not a loosening (to trip it, use "
                                      "`shortbot kill-switch`)"}
    try:
        hold = rearm.preview(j, args.rearm_margin)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    why = confirm(f"kill switch {st['state']} since {st.get('at')} by {st.get('by')}: {st.get('reason')}\n"
                  f"after the clear, the loss trips hold at what you accept now:\n{rearm.describe(hold)}\n"
                  f"set it to {args.to}")
    if why:
        return {"ok": False, "error": why}
    return await ks.clear(j, s, args.to, _by(), args.reason, shown, hold)


async def reset_peak(j: Journal, s: Settings, reason: str, equity_fn) -> dict[str, Any]:
    """Drawdown is measured from today's live equity from now on. Swaps only the peak the human saw: a new high or a
    transfer the guardian records meanwhile means nothing changes and they re-run."""
    if not (reason or "").strip() or _under_claude():
        return {"ok": False, "error": _under_claude() or "equity-peak reset needs a --reason"}
    equity = await equity_fn(s)
    if not equity or equity <= 0:
        return {"ok": False, "error": f"live equity is {equity}: nothing to reset the peak to"}
    key = j.mode_key(equity_marks.KV_PEAK)
    shown = j.get(key)
    peak = float(shown) if shown else None
    dd = f"drawdown {(peak - equity) / peak * 100:.1f}%" if peak else "no peak yet"
    why = confirm(f"equity peak {shown or 'unset'}, live equity {equity:.2f} ({dd})\n"
                  f"reset the peak to {equity:.2f}, so drawdown is measured from here")
    if why:
        return {"ok": False, "error": why}
    j.db.commit()
    j.db.execute("BEGIN IMMEDIATE")   # check-and-reset under one write lock (adjusted_peak takes the same)
    try:
        now = j.get(key)
        if now != shown:
            j.db.rollback()
            return {"ok": False, "error": f"the equity peak changed while you were confirming ({shown} -> {now}): "
                                          "nothing changed, re-run"}
        out = equity_marks.reset_peak(j, equity, f"{reason.strip()} ({_by()})")   # its first write commits
    except Exception:
        if j.db.in_transaction:
            j.db.rollback()
        raise
    await ks.notify(j, s, "equity_peak_reset", f"equity peak {shown} -> {equity:.2f} by {_by()}: {reason}",
                    urgent=False)
    return out


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shortbot-admin", description="Human-only kill-switch controls. Run it in "
                                "your own terminal, not through Claude.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="state, who set it and why, guardian heartbeat")
    c = sub.add_parser("clear", help="loosen the kill switch (asks for a code on this terminal)")
    c.add_argument("--to", choices=(ks.ACTIVE, ks.REDUCING), default=ks.ACTIVE)
    c.add_argument("--reason", required=True, help="what you reviewed and why trading may resume")
    c.add_argument("--rearm-margin", type=float, default=rearm.REARM_MARGIN_PP,
                   help=f"percentage points a held loss (daily loss, drawdown) must worsen before it trips again "
                        f"(0..{rearm.MAX_MARGIN_PP}, default {rearm.REARM_MARGIN_PP})")
    e = sub.add_parser("equity-peak", help="reset the drawdown peak to today's live equity (asks for a code on "
                                           "this terminal)")
    e.add_argument("action", choices=("reset",))
    e.add_argument("--reason", required=True, help="what you reviewed (e.g. a demo top-up, a reviewed drawdown)")
    return p


def main() -> None:
    out = asyncio.run(run(parser().parse_args()))
    print(json.dumps(out, indent=2, default=str))
    if out.get("ok") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

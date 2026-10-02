"""One-off backfill (#32): limit fills journalled before #32 carry no experiment. The guardian that journals a fill has
no research params, so `by_experiment` counted them as baseline although the experiment sized them (in the demo
journal of 2026-10-01, 9 of 13 trades). This finds the experiments and limits in force when each fill's entry was
PLACED, from the set_params / reset_params / params_dropped decisions, and writes them onto the trade.

    python -m shortbot.params_backfill [--db PATH]            # dry run: read-only, prints the plan
    python -m shortbot.params_backfill [--db PATH] --apply    # writes it (only trades still without an experiment)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVENTS = ("set_params", "reset_params", "params_dropped")


def _ts(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def plan(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """[{trade_id, symbol, placed_at, experiment, limits, decision_id}] for limit fills without an experiment whose
    entry was placed while an experiment was active (in the trade's own mode). Read-only."""
    db.row_factory = sqlite3.Row
    if not {"trades", "entries", "decisions"} <= {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        return []
    mode = "t.mode" if "mode" in {r[1] for r in db.execute("PRAGMA table_info(trades)")} else "'demo'"
    trades = db.execute(f"SELECT t.id, t.symbol, {mode} AS mode, MIN(e.created_at) AS placed_at FROM trades t"
                        " JOIN entries e ON e.trade_id = t.id WHERE t.experiment IS NULL GROUP BY t.id ORDER BY t.id")
    events = sorted((_ts(d["ts"]), d["id"], d["action"], json.loads(d["snapshot"] or "{}")) for d in db.execute(
        f"SELECT id, ts, action, snapshot FROM decisions WHERE action IN ({','.join('?' * len(EVENTS))})", EVENTS))
    out = []
    for t in trades.fetchall():
        placed = _ts(t["placed_at"])
        prior = [e for e in events if e[0] <= placed and (e[3].get("mode") or "demo") == t["mode"]]
        if not prior:
            continue
        _, did, action, snap = prior[-1]
        label = snap.get("active") if "active" in snap else snap.get("experiment")
        if action == "reset_params" or not label:
            continue   # the baseline was in force: no experiment is right
        out.append({"trade_id": t["id"], "symbol": t["symbol"], "placed_at": t["placed_at"], "experiment": label,
                    "limits": snap.get("after"), "decision_id": did})
    return out


def apply(journal, rows: list[dict[str, Any]]) -> int:
    """Write the plan; a trade that got an experiment meanwhile is left alone. Logged as one decision."""
    n = 0
    for r in rows:
        t = journal.get_trade(r["trade_id"])
        if not t or t.get("experiment") is not None:
            continue
        ef = t.get("entry_features") if isinstance(t.get("entry_features"), dict) else {}
        journal.update_trade(r["trade_id"], experiment=r["experiment"], entry_features={
            **ef, "limits": ef.get("limits") or r["limits"], "attribution_backfill": {"decision_id": r["decision_id"]}})
        n += 1
    if n:
        journal.log_decision("experiment_backfill", f"{n} limit fill(s) tagged with the experiments active when "
                             "their entry was placed (#32)", snapshot={"rows": rows})
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m shortbot.params_backfill", description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", help="journal path (default: SHORTBOT_DB / data/shortbot.db)")
    ap.add_argument("--apply", action="store_true", help="write the plan (default: dry run, read-only)")
    args = ap.parse_args(argv)
    if args.db:
        path = Path(args.db)
    else:
        from .config import Settings
        path = Settings.from_env().db_path
    if not path.exists():
        print(f"no journal at {path}", file=sys.stderr)
        return 1
    if not args.apply:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = plan(con)
        finally:
            con.close()
        print(json.dumps({"dry_run": True, "would_tag": len(rows), "rows": rows}, indent=1, default=str))
        return 0
    from .journal import Journal
    j = Journal(path)
    rows = plan(j.db)
    print(json.dumps({"applied": apply(j, rows), "rows": rows}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

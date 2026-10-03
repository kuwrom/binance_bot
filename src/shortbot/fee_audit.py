"""Read only (#75): the journalled fees and P&L of closed trades against Binance income matched the corrected way
(reconcile.trade_income). Before #75, finalize read income from the entry fill's own ms, so the entry COMMISSION,
stamped to the second before its fill (demo, probe_result 395), was missing from every trade closed through its trade
history (reconcile, round trips). A re-entry soon after a close could also add its rows to the closed trade. Since #76
a trade's position is found by its journalled order ids where it has them (`matched`: "ids"; a trade from before then:
"time", episodes.match), and funding goes to the position that held it. Nothing is written: the human decides whether
to correct history.

    .venv/bin/python - <<'PY'
    from shortbot import lab
    r = lab.fee_audit()
    print(r["totals"], r["audited"], r["skipped"])
    for x in r["trades"]:
        print(x["id"], x["symbol"], x.get("matched"), x.get("diff") or x.get("skipped"))
    PY
"""

from __future__ import annotations

from typing import Any

from . import episodes, reconcile

FIELDS = ("gross", "fees", "funding", "net")


def _read_until(trade: dict[str, Any]) -> int | None:
    """Trade history past its close: closed_at is its exit fill's second or later, plus the settle window."""
    return reconcile.iso_to_ms(trade["closed_at"]) + episodes.SETTLE_MS + 1000 if trade.get("closed_at") else None


async def audit(tr, limit: int = 500) -> dict[str, Any]:
    """Every closed trade of the journal's mode (the last `limit` trades): before = the journal's gross_pnl, fees,
    funding and pnl; after = the same from income matched to its position's fills; diff = after - before; matched =
    how its position was found ("ids" or "time"). A trade whose position trade history can't show (or whose read
    fails) is listed with `skipped` and its reason.
    tr: anything with `t` (the trade client) and `j` (the journal)."""
    out: list[dict[str, Any]] = []
    totals = dict.fromkeys(FIELDS, 0.0)
    for trade in [x for x in tr.j.trades(limit) if x["status"] == "closed"]:
        row = {"id": trade["id"], "symbol": trade["symbol"], "opened_at": trade["opened_at"],
               "closed_at": trade.get("closed_at")}
        try:
            ep = await episodes.match(tr, trade, _read_until(trade))
            if ep["state"] != "closed":
                out.append({**row, "skipped": f"trade history shows no closed position for it ({ep['state']})"})
                continue
            inc = await reconcile.trade_income(tr.t, trade, ep)
        except Exception as e:  # noqa: BLE001 - reported on its row; the other trades are still audited
            out.append({**row, "skipped": f"{type(e).__name__}: {e}"})
            continue
        before = {"gross": trade.get("gross_pnl"), "fees": trade.get("fees"), "funding": trade.get("funding"),
                  "net": trade.get("pnl")}
        after = {"gross": inc["gross"], "fees": inc["fees"], "funding": round(inc["funding"] + inc["liq_fee"], 6),
                 "net": inc["net"]}
        diff = {k: round(after[k] - float(before[k] or 0), 6) for k in FIELDS}
        for k in FIELDS:
            totals[k] += diff[k]
        out.append({**row, "matched": ep.get("by"), "before": before, "after": after, "diff": diff})
    skipped = sum(1 for x in out if "skipped" in x)
    return {"trades": out, "audited": len(out) - skipped, "skipped": skipped,
            "totals": {k: round(v, 6) for k, v in totals.items()}}

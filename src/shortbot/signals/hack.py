"""Hack / exploit shadow flag (task #20; library card hack_exploit_short). NY Fed SR 1153: across 49 DeFi hacks the
24h CAR is about -27%, with ~-13 pp after public disclosure. Research feature only: no points, no vetoes. The
matched headline, source and URL are logged so false positives can be audited."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ..market import MarketContext
from ..news import source_of
from ..untrusted import wrap
from .base import SignalOut, signal

MAX_AGE_H = 72.0
HACK_RE = re.compile(r"\b(hack(ed|er|ers|s)?|exploit(ed|er|s)?|drain(ed|s)?|attack(ed|er|s)?|breach(ed|es)?|"
                     r"stolen|compromised)\b", re.I)
REIMBURSE_RE = re.compile(r"\b(reimburs\w*|compensat\w*|refund\w*|recover(ed|y)|returned|bounty|white ?hat)\b",
                          re.I)


def _age_h(iso: str | None, now: datetime) -> float | None:
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 3600 if iso else None
    except ValueError:
        return None


def match(items: list[dict], now: datetime) -> dict:
    """Newest headline (title only) with exploit vocabulary within MAX_AGE_H, plus whether ANY recent item about the
    coin talks about reimbursement (the squeeze risk for a short)."""
    recent = [(h, a) for h in items if (a := _age_h(h.get("time"), now)) is not None and a <= MAX_AGE_H]
    hits = [(h, a) for h, a in recent if HACK_RE.search(h.get("title") or "")]
    if not hits:
        return {"hack_flag": False}
    h, a = min(hits, key=lambda x: x[1])
    return {"hack_flag": True, "hack_headline": wrap(h.get("title"), source_of(h), 200),   # third-party text
            "hack_source": h.get("source") or "Binance",
            "hack_url": h.get("url"), "hack_age_h": round(a, 1), "hack_hits": len(hits),
            "hack_reimbursement": any(REIMBURSE_RE.search(x.get("title") or "") for x, _ in recent)}


@signal("hack_flag", "event")
async def hack_flag(ctx: MarketContext) -> SignalOut:
    """A Binance announcement or RSS headline from the last 72h that names this coin AND uses exploit vocabulary
    (hack, exploit, drained, attack, breach, stolen, compromised): flag, headline, source, URL, age, and whether a
    reimbursement/bounty is being discussed. Shadow feature only: no points, no vetoes."""
    n = await ctx.news()
    items = list(n.get("binance_announcements") or []) + list(n.get("headlines") or [])
    return SignalOut(features=match(items, datetime.now(timezone.utc)))

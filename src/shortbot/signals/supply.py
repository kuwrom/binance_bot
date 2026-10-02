"""Listing supply tags (task #15; learning #36; library cards airdrop_hodler_supply_short, lowfloat_highfdv_decay_short,
supply_overhang_short). Research features only: no points, no vetoes, until research_report shows an effect on
enough independent symbol-episodes.

- float / FDV from the guardian's weekly CoinGecko cache (market/supply.py), for every coin;
- the listing's supply source for perps listed <= 90 days ago, from Binance's new-listing announcements
  (news.listing_announcements: one cached fetch per 6h for all coins). Most specific source first."""

from __future__ import annotations

import re

from ..market import MarketContext
from .base import SignalOut, signal

MAX_AGE_D = 90
SOURCES = (("hodler_airdrop", re.compile(r"HODLer Airdrops?", re.I)),
           ("launchpool", re.compile(r"Launchpool", re.I)),
           ("megadrop", re.compile(r"Megadrop", re.I)),
           ("alpha", re.compile(r"Binance Alpha(?! Will Remove)", re.I)),
           ("seed_spot", re.compile(r"Will List .*Seed Tag", re.I)),
           ("spot", re.compile(r"Will List\b", re.I)),
           ("futures_only", re.compile(r"Futures Will Launch", re.I)))


def classify(titles: list[str]) -> str | None:
    """The most specific source any announcement shows (a HODLer airdrop beats a plain spot listing, which beats a
    futures-only launch). None when nothing matches."""
    for name, pat in SOURCES:
        if any(pat.search(t) for t in titles):
            return name
    return None


def supply_facts(s: dict | None) -> dict:
    s = s or {}
    circ, total, mx = s.get("circulating_supply"), s.get("total_supply"), s.get("max_supply")
    denom = total or mx
    mcap, fdv = s.get("market_cap"), s.get("fully_diluted_valuation")
    return {"float_ratio": round(circ / denom, 4) if circ and denom else None,
            "fdv_usd": fdv, "mcap_usd": mcap, "fdv_mcap": round(fdv / mcap, 2) if fdv and mcap else None}


@signal("listing_supply", "event")
async def listing_supply(ctx: MarketContext) -> SignalOut:
    """Float ratio (circulating / total, else max), FDV, market cap and FDV/mcap from the CoinGecko cache; for perps
    listed <= 90 days ago the listing's supply source (hodler_airdrop / launchpool / megadrop / alpha / seed_spot /
    spot / futures_only) with the announcement that set it. Features only (research): no points, no vetoes."""
    feats: dict = {**supply_facts(await ctx.supply()), "listing_source": None, "listing_source_title": None}
    age = (await ctx.market_info()).get("listing_age_days")
    if age is not None and age <= MAX_AGE_D:
        anns = await ctx.listing_news()
        src = classify([a["title"] for a in anns])
        if src:
            pat = dict(SOURCES)[src]
            hit = next(a for a in anns if pat.search(a["title"]))
            feats.update(listing_source=src, listing_source_title=hit["title"][:160],
                         listing_source_time=hit.get("time"))
    return SignalOut(features=feats)

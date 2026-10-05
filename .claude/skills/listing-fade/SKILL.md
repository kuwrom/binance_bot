---
name: listing-fade
description: Playbook for shorting newly listed Binance perpetuals that drift down over days to weeks - timing, entry after the initial volatility, wide stops, long time stop.
when_to_use: A candidate is tagged listing_fade (perp listed < 30 days), the scan shows new_listings_30d, or Binance announces a new futures listing.
---

# listing_fade

**Thesis:** new perps tend to drift down over weeks. One study of 470 Binance listings: median −4.5% at 1d, −11.5% at 7d, −21.5% at 30d, and only about 29% up at 30d. The fade is strongest in high-turnover listings.
**Evidence:** github.com/Asalio123/binance-listing-study (single open-source study, moderate trust), Robot James.
**Status:** active. Research data from `research_report.by_setup.listing_fade` beats this page.

## v2 rules (2026-09-29, learnings #15 and #18: our 267-listing study agrees with the literature)
- **Only spot-listed launches.** That means HODLer airdrop or spot opened with the perp (`spot_available` true). The detector enforces it.
- **Never** Alpha or perp-only listings. 40% of them run +100% before falling, so treat those as `pump_fade` after a confirmed turn.
- **Enter around day 7** (day 6-30), or on the first close 30% below the peak. Day 0-3 shorts have fatter squeeze tails, and the edge fades after day 14 for fresh entries.
- **Hold 30-60 days** (default time stop is 45 days). Use a wide stop at the max allowed distance, 1-2x leverage, and **conviction C**.
- **Better when** first-week volume was high and BTC is falling. Budget for funding: new listings often charge shorts.

## Checklist
- Listing age 6–30 days (`coin_snapshot.listing.listing_age_days`; the detector's window). Day 0-3 shorts have fatter squeeze tails.
- High turnover: the fade concentrates in the most traded listings. Quiet listings show no fade.
- Negative funding on new perps: half size, and the funding budget applies (learning #22). Check `funding_history_pct`: a long hold pays funding every interval.
- Not a Binance Alpha or low-float token (scanner veto): squeezes of +100–1,400% happen there.

## Entry
- After an initial pump fails: a lower high on 4h/1h, or the price losing the listing-day VWAP.
- This setup works on a daily scale, so no rush. Wait for structure.

## Stop and size
- Use the wide stop (or a daily swing high) and smaller size.
- 1–2× leverage. Distance matters more than precision here.

## Default plan
- time_stop_hours 1080 (45 days; setups.py)
- breakeven at +10%, trail 10% once +20%
- review every 24h
- Targets: −20% and −35% from entry are reasonable starting points; record what actually happens.

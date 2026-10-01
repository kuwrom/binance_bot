---
name: pump-fade
description: Playbook for fading an overextended Binance futures top-gainer after a confirmed turn - froth checklist, entry triggers, stops, default plan, squeeze vetoes.
when_to_use: A scan candidate is tagged pump_fade, or the user asks about shorting a coin that pumped.
---

# pump_fade

**Thesis:** small and mid caps that spike on futures tend to mean-revert. Majors keep trending, so don't fade them.
**Evidence:** FMZ workflow [S], small-cap reversal study [W]; see docs/PLAYBOOK.md.
**Status:** active. Check `journal` → `by_setup.pump_fade` and the learnings; they override this page.

## Froth: 3 or more of these
- 24h change ≥ +10%, stronger ≥ +30%
- funding ≥ +0.05%/8h (extreme ≥ +0.10%)
- OI up ≥ 30% together with price (late longs)
- 1h RSI ≥ 80
- ≥ 2 SD above the 24h VWAP
- 1h volume ≥ 3× average

## Trigger: required, at least one confirmed
- lower high on 1h (and 15m)
- loss of VWAP or EMA20 on 15m/1h, with a failed retest
- taker buy/sell flipped below 1 while price is at highs
- a rejection wick into the +2/+3 SD band, entering on the break of that candle

**No trigger means a watchlist entry, not a trade.**

## Stop and size
- `coin_snapshot.suggested_stops`: tight (4h swing + 1× ATR) or wide (12h swing + 1.5× ATR).
- Size is automatic: wider stop, smaller position.
- Leverage must satisfy liquidation > stop + buffer (the code enforces this).

## Default plan (override per trade)
- time_stop_hours 12
- breakeven at +5%, trail 4% once +10% (automatic)
- Suggested targets:
  - first target: the pre-pump base or the 0.5 retrace of the pump, half the position
  - runner: 0.618 retrace
- kill_conditions: "new high on rising OI", "funding turns negative", "bullish catalyst in news"

## Vetoes specific to this setup
- the pump has a real catalyst (listing on a major spot exchange, partnership, hack recovery), so read `news`
- the coin is already down more than 20% from its high (the fade is done; look for a bounce-short later instead)

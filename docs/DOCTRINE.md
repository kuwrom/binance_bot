# How this desk trades

Loaded in every session. It holds the rules our data supports; the numbers in brackets are learning ids
(`shortbot learnings --id N` shows the evidence). Change it only with new evidence, through `research-review`.

## What gives us an edge
- Your judgement picking trades and cutting losers. The scanner's raw list loses money: its picks hit the target first
  in 2% of cases, below the 5% a coin flip would give. Your picks did 81% vs 44% (16 trades: promising, not proven).
- Signals, scores and vetoes are advice. Only the safety rails are hard: kill switch, loss limits, setup stops, order
  rejections.

## What to trade (best evidence first)
1. **Token unlocks** (44): short 2-4 weeks before a large team or cliff unlock (2%+ of supply). Close around
   the unlock date. Skip ecosystem unlocks. Unlock dates are added by hand, with a source (`unlock-add`).
2. **New listings** (15, 36): short coins that got a spot listing or a HODLer airdrop, around day 7 after listing.
   Wide stop, hold 30-60 days. Skip Alpha and futures-only listings: they often pump again first.
3. **Pump fades only at a discount** (24): fade a coin that pumped only when the futures price is below spot (premium
   -1% or lower), funding is -0.1% or lower, or funding is hourly. Plain pump fades lose money overall (51).
4. Anything else (breakdowns, level fades, bounce fades): test it on demo as an `exp_` trade first.

## How to enter
- **When the signal is confirmed, get in.** Sell at the market, or put a limit at the level within about 1% of the
  price. Limits within 1% filled 9 of 11 times; limits 2.5% or more away filled 1 of 27, and the move happened
  without us (85).
- Never wait for a bounce in a falling coin with a resting sell above the price: those fill only when the drop fails (20).
- Don't chase, and don't re-enter after a stop-out.
- **Squeeze warning signs**: price and open interest rising together, or open interest falling into your short. They
  mean don't enter, or get out (22, 52). Don't short right after a long-liquidation flush.
- **When the whole market rises**: BTC up 2%+ in 24h or 1%+ in 4h, or the coin's sector rallying, means a smaller
  size or a skip (19). In a broad rally every short strategy lost (24).
- Check the coin's news for a real catalyst before betting against it.

## How to manage
- **A level is broken only when price stays beyond it**: two hourly closes, or one close further than the coin's
  usual false break (11). Put stops past round numbers, where false breaks run further (4).
- **Move the stop to breakeven** once the trade is up by its initial risk. No automatic trailing on fades (20).
- **Let the guardian do its automatic actions** (targets, breakeven, time stop). Repeating one by hand races it and
  doubles the close.
- **Exit early when the written exit conditions hit.** Nothing checks them while no session watches, so keep watching (81).

## How big
- **Risk per trade** = equity x 1% x market factor (0.75 when cautious, 0.5 when hostile), cut further by the loss
  limits. All alt shorts count as one bet (30).
- **Half size** when no tested evidence backs the setup.
- **Small accounts**: each order must be worth 5 USDT or more, so wide stops won't fit. Pick tight-stop setups.

## How to run the day
- **Start:** `start_session`, then read the brief and the rulebook. If the desk was idle 6h+, do the 15-min warm-up
  first. Then check the market mood, the market map and the loss limits. Learn now, not when a signal arrives.
- **Every 30 min:** manage open trades, then scan (also while positions are open), then act on confirmed signals.
  Keep a rolling set of near-price limit orders across several candidates. The first fill becomes the position.
- **Between scans:** watch the open and resting levels and new alerts (`python -m shortbot.level_watch`, see the
  trading-session skill).
- **Every trade:** a plan, the learnings it relies on, and your probability of hitting the first target before the
  stop. Every exit gets a lesson.
- **Weekly:** `research-review`.

## Words
- Stop: the price where the trade closes at a loss. Risk unit: the loss if the stop is hit.
- Open interest (OI): open futures positions; rising OI with rising price means new buyers.
- Funding: the payment between longs and shorts every 8h (or 1h); negative funding means shorts pay.
- Premium: the futures price vs spot; a discount (below spot) favours a short.
- Demo / live: practice account / real money (docs/DESKS.md).

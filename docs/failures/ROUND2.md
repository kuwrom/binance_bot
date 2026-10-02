# Round 2 (2026-09-29)

> **Statistics note (#31, 2026-10-02):** the clustered t-statistics and Deflated Sharpe ratios quoted here come from the **pre-#31 estimator**: a t over equal-weighted day (or coin / hour) means, not the t of the pooled mean reported beside it, and a DSR with T = the trade count and N = one run's trials. They are kept as recorded. A rerun reports the t of the pooled mean (day, coin and two-way) and a DSR on the effective sample with N from the trial register: docs/BACKTEST.md "Clustering and multiple testing".

Three questions from the human-approved list: (1) which **entry type** in breakdowns, (2) whether **auto breakeven / trailing** add value on our fades, (3) **re-entry** after a stop-out and a **funding budget** for discount shorts. They are comparisons on identical signals, not new filters, so the IS step below is a description, not a search.

### 1. Diagnosis (in-sample only: signals ≥ 30h before the 2026-08-29 19:30 UTC midpoint, entries before it)
- **Entry type (Q1).** On the same breakdown arms, the resting limit at the EMA20 filled 43% (drop −15%) / 50% (−8%) of arms and lost **−0.32R / −0.31R** per fill; 64% / 60% of fills were stopped. Market at the arm lost −0.15 / −0.25R, the sell-stop −0.13 / −0.17R (fill 32% / 29%). The limit's fills are adversely selected: they happen when the bounce keeps going (Linnainmaa 2010).
- **Exits (Q2).** Post-entry paths do **not** trend. Over the time-stop window with only the stop honoured:
  - pump_fade: 1h return autocorrelation −0.07; once +1R is reached, price reaches +2R before returning to entry **45%** of the time (a random walk gives 50%).
  - post_pump_decay: ac1 −0.02, P(+2R first | +1R) 0.32. bounce_fade: ac1 −0.09, 0.50. pump_fade+discount: ac1 +0.01, 0.50 (n 18).
  - Paired on identical signals, breakeven at +1R vs fixed exits: +0.007R (pump_fade), +0.001 (post_pump_decay), +0.02 (bounce_fade, n 116). Trailing at the live % parameters: +0.002, −0.002, −0.012. Kaminski & Lo (2014) predict exactly this: without positive serial correlation a stop rule cannot add return.
- **Re-entry (Q3).** 277 IS pump_fade stop-outs closed back below the stop within 3 bars; re-shorting them averaged −0.14R (n 12, +0.11R, on pump_fade+discount).
- **Funding budget (Q3).** 0 IS trades paid ≥ 0.5R of funding in a 12h hold, on either base. Expect the rule to be a no-op.

### 2. Pre-registered rules (written before the OOS run; code `strategies_v3.py`)
**Q1: entry type on one breakdown signal.** Armed at the close of bar i when all hold: 24h return ≤ drop (−15%, round 0's bounce_fade pick, and −8%, the live `breakdown` detector); lower high (bounce_fade's rule: high of the last 12 bars < high of bars i−36..i−13); close below EMA20 on bars i−1 and i; bar i's high still below EMA20 (no bounce yet); tradable ($1M/24h). At most one arm per coin per 12 bars.

| | entry | fill rule |
|---|---|---|
| (a) limit | resting sell at EMA20ᵢ (the live bounce-sell) | a later bar trades through it within 12 bars; maker fee; the fill bar is checked for the stop only |
| (b) sell-stop | 0.1% under the 12h breakdown low (bars i−11..i) | live only after a bounce attempt (a later bar trades ≥ closeᵢ + 0.5 ATR); fills on the first later bar trading below the trigger, at the trigger or the gapped open; taker fee + 2× slippage (stop-market); cancelled if the stop trades first; 12-bar TTL |
| (c) market | the arm bar's close | always |

All three share the stop, fixed at the arm: max(EMA20ᵢ, high of bars i−2..i) + 0.5 ATR. Targets are 1.5R (half) and 3R (rest) of each entry's own risk; breakeven at 1R, trail 2R/1R, 24h time stop (bounce_fade's exits). The same-hour placebo enters at the **fill** bar. Reported: fill rate, R per arm (unfilled = 0), and the market-at-arm R on the arms each resting order did / did not fill (adverse selection).

**Q2: exits on identical signals** for pump_fade (1 ATR, 12h), pump_fade+discount, post_pump_decay (2 ATR, 12h) and bounce_fade (−15%, 24h):
- (i) fixed: stop + targets + time stop only.
- (ii) + breakeven at +1R (the backtest's current pump-family default).
- (iii) + trailing at the **live guardian's** parameters (`setups.py`): pump family 10% activation / 4% trail, bounce_fade (the `breakdown` setup) 5% / 3%, converted to R per trade.
- (iv) both.
- The existing trials already cover (ii) for the pump family, and bounce_fade's current exits (1R breakeven + 2R/1R trail) are kept as a reference row.
- Reported: paired Δ vs (i) on the same signals (day-clustered t, IS/OOS), and the continuation stats above on the (i) trades and their same-hour placebo.

**Q3** on pump_fade and pump_fade+discount:
- **Re-entry:** after a stop-out at the original stop S, re-short at the close of the first of the next 3 bars that closes below S. New stop = the highest high since the stop bar + 1 ATR (the base buffer); the original target prices; 12h time stop; breakeven 1R. One re-entry per trade. The trial is the base book plus its re-entries.
- **Funding budget:** exit at a bar's close once the funding paid is ≥ 0.5R while the open price gain is < 0.5R.

**Ledger:** 23 new trials (Q1 6, Q2 13, Q3 4), for a total of **N = 76** in the DSR.

**Pass bar:** the round-1 verdicts (SURVIVES / adopt-to-shadow / needs more data at OOS n < 30 / reject). The comparative calls are fixed now:
- **Q1:** the desk uses the entry type with the best OOS **R per arm**, provided its fills beat the same-hour placebo in both halves. If none does, use the least negative one and say the breakdown short is not established.
- **Q2:** a management rule stays on for a family only if its paired Δ vs fixed is > 0 in **both** halves. If Δ < 0 OOS, turn it off. If |Δ| < 0.02R in both halves, it is neutral and the choice is operational (fewer moving parts).
- **Q3:** adopt re-entry to shadow only if the re-entry trades average > 0 in both halves and beat their same-hour placebo. The funding budget is "needs data" if it fires on fewer than 10 trades.

<!-- round-2 results below were added AFTER the single OOS run -->

### 3. OOS results: one run, 2026-09-29 (`--round2`, N = 76 trials, V[SR] = 0.0122, so SR\* ≈ 0.27 per trade)
The round-0/1 rows reproduce exactly (pump_fade 2211 trades, −0.09 / −0.06; pump_fade+discount 133, +0.07 / +0.17). With 76 trials and a wider spread of trial Sharpes, pump_fade+discount's **DSR falls from 0.54 to 0.23**. Columns are as in round 1; "per arm" counts an unfilled order as 0R.

**Q1: entry type on the same breakdown arms**

| drop | entry | fill % | R per arm, IS / OOS / all | R per fill, IS / OOS (n) | edge vs same-hour plc (t) IS/OOS | stress R per fill | verdict |
|---|---|---|---|---|---|---|---|
| −15% | (a) limit at EMA20 | 41 | −0.14 / **+0.00** / −0.08 | −0.33 / +0.01 (173) | +0.28 (3.5) +0.22/+0.37 | **−0.35** | reject |
| −15% | (b) sell-stop | 32 | **−0.04** / −0.01 / **−0.03** | −0.14 / −0.03 (127) | +0.15 (1.7) +0.14/+0.17 | −0.12 | reject |
| −15% | (c) market | 100 | −0.16 / −0.05 / −0.12 | −0.16 / −0.05 (342) | +0.14 (4.5) +0.13/+0.17 | −0.15 | reject |
| −8% | (a) limit at EMA20 | 45 | −0.16 / −0.09 / −0.12 | −0.30 / −0.23 (805) | +0.24 (5.5) +0.24/+0.25 | **−0.48** | reject |
| −8% | (b) sell-stop | 30 | **−0.05** / −0.09 / **−0.07** | −0.17 / −0.28 (561) | +0.09 (3.5) +0.15/+0.03 | −0.28 | reject |
| −8% | (c) market | 100 | −0.25 / −0.17 / −0.21 | −0.25 / −0.17 (1651) | +0.08 (5.8) +0.07/+0.09 | −0.27 | reject |

**Adverse selection, measured directly** (the market-at-arm trade on arms where the resting order did / did not fill):
- Limit, −15%: the market trade made **−0.70R** on arms where the limit filled vs **+0.28R** where it did not (n 336 / 503). At −8%: −0.70 vs +0.13.
- Sell-stop, −15%: +0.13 where it filled vs −0.22 where it did not. At −8%: +0.02 vs −0.30.
- So the limit fills exactly on the arms that fail: the bounce that reaches the EMA keeps going. The sell-stop fills on the arms that work.
- The two orders' prices pull the other way. Paired on the same arm, limit − market is +0.45R (t 6.8) and sell-stop − market is −0.23R (t −5.2): a worse fill plus 2× slippage.
- Net, the sell-stop has the best R per arm over the window and IS in both drops, and the best stress R (−0.04 per arm vs −0.14 for the limit at −15%). OOS it ties the limit: +0.00 vs −0.01 at −15%, −0.09 vs −0.09 at −8%.
- Under the literal pre-registered rule (best OOS R per arm), the limit wins at −15% by 0.011R, which is inside noise. It also loses 0.33R per fill IS and 0.35R per fill under stress, because its stop sits only 0.5 ATR above the fill.

**Q2: exits on identical signals.** Δ is paired against (i) fixed exits, day-clustered t, IS/OOS.

| family | (ii) + breakeven 1R | (iii) + live trail | (iv) both | OOS R (i) → (ii) / (iii) / (iv) |
|---|---|---|---|---|
| pump_fade | **+0.01 (t 3.3)** +0.01/+0.01 | +0.00 (0.6) +0.00/−0.00 | +0.01 (3.1) +0.01/+0.01 | −0.07 → −0.06 / −0.07 / −0.07 |
| pump_fade+discount | +0.01 (1.4) +0.02/+0.00 | −0.00 (−0.8) +0.01/−0.01 | +0.00 (−0.2) +0.02/−0.01 | +0.17 → +0.17 / +0.15 / +0.15 |
| post_pump_decay | +0.00 (−0.3) | −0.00 (−0.0) | −0.00 (−0.0) | −0.11 → −0.11 / −0.11 / −0.11 |
| bounce_fade | +0.02 (−0.6) +0.02/+0.01 | **−0.05 (t −2.1) −0.01/−0.12** | −0.03 (−1.7) +0.01/−0.10 | +0.13 → +0.13 / +0.04 / +0.06 |

bounce_fade's current backtest exits (breakeven plus a 2R/1R trail) come out at +0.03 vs fixed. It is the live breakdown trail (5% activation / 3% distance) that hurts, not trailing as such. Trail exits are 54% of bounce_fade trades under it.

**Why exits add nothing: post-entry continuation.** The fixed-exit trades, over the time-stop window, honouring only the stop:

| family | ac1 (1h), IS / all / same-hour plc | P(+2R before back to entry, after +1R), IS / all / plc | gave back to entry after +1R |
|---|---|---|---|
| pump_fade | −0.07 / −0.06 / −0.02 | 0.46 / **0.43** / 0.38 | 26% |
| pump_fade+discount | +0.00 / −0.04 / −0.05 | 0.50 / 0.44 / 0.50 | 12% |
| post_pump_decay | −0.02 / +0.01 / −0.03 | 0.32 / **0.37** / 0.45 | 12% |
| bounce_fade | −0.04 / −0.05 / −0.03 | 0.49 / 0.51 / 0.39 | 37% |

- The hourly autocorrelation after entry is ≤ 0 in every family.
- Once +1R is in hand, reaching +2R before returning to entry is at or below a coin flip (0.37–0.51). This is mild mean reversion, not momentum.
- By Kaminski & Lo, a stop or trail cannot add expected return on such paths. It only trims variance, or, when it is tight (bounce_fade's 3% trail), it cuts winners.
- Breakeven barely matters because few trades return to entry after +1R before a target or the time stop takes them out.

**Q3: re-entry and funding budget**

| trial | n | IS R | OOS R (n) | edge (t) IS/OOS | stress | t_day | DSR | added trades: n, avg R IS/OOS | verdict |
|---|---|---|---|---|---|---|---|---|---|
| pump_fade + re-entry | 2720 | −0.10 | −0.08 (1352) | +0.10 (4.1) +0.11/+0.08 | −0.15 | −1.8 | 0.00 | 525 re-entries, −0.15: −0.14/−0.17 | **reject** |
| pump_fade+discount + re-entry | 154 | +0.08 | +0.10 (78) | +0.20 (5.4) +0.22/+0.18 | +0.07 | 3.5 | 0.07 | 21 re-entries, −0.10: +0.11/**−0.38** | **reject** (book OOS +0.17 → +0.10) |
| pump_fade + funding budget | 2211 | −0.09 | −0.06 (1114) | = base | −0.13 | −2.0 | 0.00 | 0 exits fired | **needs data** (no-op) |
| pump_fade+discount + funding budget | 133 | +0.07 | +0.17 (69) | = base | +0.10 | 3.2 | 0.23 | 0 exits fired | **needs data** (no-op) |

- The re-entries lose in both halves on pump_fade. The price that closes back under the stop is not a new failed sweep: most of these continue up.
- With a 12h hold and 8h prints, no trade ever paid 0.5R of funding, so the budget never fired. It can only be tested on the long-hold shadow log (1h-interval coins held for days), as round 1 said.

#### Verdicts
- **Q1 entry type:** every breakdown variant is **rejected** as a strategy: IS R is ≤ −0.14, OOS R is ≤ +0.01 per fill, and stress R is ≤ −0.12. The relative edge vs the same hour is real (+0.08 to +0.28R).
  - **Desk rule: stop placing resting bounce-sell limits at the EMA20.** Their fills are the failed arms: the market trade on those arms lost 0.70R, in both drops and both halves.
  - If a breakdown short is taken, enter by **sell-stop under the breakdown low after a bounce attempt**. It is the least negative per arm over the window, IS and under stress, and it ties the limit OOS.
  - The breakdown short itself is not established.
- **Q2 exits:**
  - **Breakeven at +1R:** keep it for the pump family. Δ is +0.01R in both halves (t 3.3 on pump_fade). It is positive but economically neutral.
  - **Trailing at the live parameters:**
    - Turn it **off for bounce_fade / breakdown**: Δ −0.05R, −0.12R OOS.
    - It is **neutral to slightly negative for the pump family**: Δ 0.00 overall, −0.01R OOS on the discount book. Leave it off, or keep it only as a risk tool, knowing it costs a little.
  - The continuation stats say why: after entry, our paths mean-revert mildly. A trail only harvests runs that don't exist at 12–24h.
- **Q3:** re-entry is **rejected** on both bases. The funding budget is **needs data**: it cannot fire at a 12h hold, so it moves to the shadow log.
- **pump_fade+discount stays the one candidate** (adopt-to-shadow). Its DSR is now 0.23 at N = 76, and its fixed-exit version is equally good (+0.17 OOS). This round did not strengthen it; it only adds trials.

### 4. Next round
1. Forward-shadow pump_fade+discount with **fixed exits plus breakeven, no trail**, and log the funding paid per trade for the budget test.
2. **Breakdown sell-stop in shadow** as a relative-edge probe only, on post-09-29 data. If the stop is placed ~1 ATR above the bounce high instead of 0.5 ATR above the EMA20, retest the limit too: its loss per fill is mostly its tight stop.
3. Hedged pump family and the retail-flip event study (round 1 items 2 and 4) remain open.


# Round 1 (2026-09-29)

### 1. Diagnosis (in-sample only)
**Sample:** the IS trades of round 0's picked param sets, re-simulated on the current `data/hist.db`: pump_fade (1 ATR, 12h) n 1097, post_pump_decay (2 ATR, 12h) n 1422, bounce_fade (−15%, 24h) n 123, and level_fade (round, market, 24h) n 4459. In total there are 7,101 trades, and the losers add up to −2,800R.

**How a loss is classified.** Each loser gets one mode; the first match below wins (`failures.failure_mode`):
- **cost:** gross R ≥ 0, so the idea broke even and fees or slippage made it a loss.
- **funding:** as cost, except the funding paid exceeds fees plus slippage.
- **b:** stopped out, and price then reached the first target within 24h. The stop was too tight or sat at the wrong level.
- **a:** stopped out within 2 bars of the entry, i.e. an instant squeeze.
- **h-stop:** stopped out later, and the target was never reached. The thesis was wrong.
- **d:** time stop, and the trade never moved: max(MAE, MFE) < 0.5R.
- **h-time:** time stop, after drifting against the short.

In the tables below, **precursor** is the as-of entry feature that best separates the mode from IS winners. AUC 0.5 means no separation; the precursor is called separable at |AUC − 0.5| ≥ 0.15.

**Failure modes: share of losing R.** The pump family is pump_fade, post_pump_decay and bounce_fade.

| mode | pump family | level_fade | precursor (mode median vs winners' median, AUC) | separable? |
|---|---|---|---|---|
| (a) instant squeeze | 13.5% | 6.5% | stop distance 4.1% vs 7.0% (0.28); distance to the 24h high 2.4% vs 5.2% (0.31); ER 0.40 vs 0.28 (0.68) | **yes**: tight stop, entry at the high, smooth run |
| (b) wick-stopped, then the target was hit within 24h | **35.3%** (pump_fade alone: **52.9%**) | 24.8% | pump family: distance to the 24h high 2.8% vs 5.2% (0.31), stop 5.1% vs 7.0% (0.35), 4h return +3.9% vs +1.4% (0.64). level_fade: **BTC 24h +4.9% vs +0.4% (0.71)**, coin 24h +7.9% vs +3.8% (0.70), ER 0.35 vs 0.21 (0.70) | **yes** |
| (h) stopped, thesis wrong | 37.7% | **56.4%** | pump family: stop 5.0% vs 7.0% (0.37), BTC 24h +0.4% vs +0.2% (0.59), taker ratio 1.14 vs 1.07 (0.58). level_fade: sector 4h (0.62), BTC 24h (0.62), BTC 4h (0.61) | weak |
| (h) time stop, drifted against | 10.1% | 10.4% | momentum already gone: 4h return +0.2% vs +1.4% (0.40), volume ratio 1.4 vs 2.3 (0.42) | weak |
| (d) flat time stop | 2.6% | 1.7% | the coin had stopped rising: 4h return −0.7% vs +1.4% (0.33) | yes, but small R |
| (e) cost-dominated | 0.7% | 0.2% | cost is mostly *inside* the stop-outs: est. cost ≥ 0.06R on 47% (pump_fade) and 59% (level_fade) of trades | as a filter, yes |
| (f) funding bleed | 0.1% | 0.0% | n/a | no: not a failure mode at 12–24h holds |

**Context labels.** These overlap with the modes. Each row gives the share of trades, the share of losing R, and avg R with the label on vs off.

| label (as-of at entry) | pump family | level_fade |
|---|---|---|
| (c) **tide:** BTC 24h ≥ +2%, BTC 4h ≥ +1% or sector 4h ≥ +1% | 24% of trades / **33% of losing R**; −0.24 vs −0.01 | 35% / **47%**; −0.31 vs −0.07 |
| (c) taker buying: hourly ratio ≥ 1.1 or 4h imbalance ≥ +0.05 | 49% / 55%; −0.12 vs −0.02 | 58% / 60%; −0.19 vs −0.11 |
| (c) smooth trend: ER ≥ 0.45 | 19% / 27%; −0.16 vs −0.05 | 15% / 22%; −0.38 vs −0.12 |
| thin: 24h volume < $5M | 39% / 45%; −0.16 vs −0.01 | 60% / 60%; −0.20 vs −0.09 |
| stop < 4% of price | 20% / 34%; −0.28 vs −0.02 | n/a (the sweep-proof stop is already wider) |
| weekday | 66% / 72%; −0.12 vs **+0.02** | 70% / 78%; −0.23 vs **+0.01** |
| (g) discount: premium ≤ −1% in 24h, funding ≤ −0.1%, or a 1h funding interval | 6% / 4%; **+0.06** vs −0.08 | 2% / 2%; **+0.12** vs −0.16 |
| (g) retail short fuel: global account L/S down ≥ 0.3 in 24h, price up | 18% / 17%; −0.03 vs −0.08 | 8% / 7%; −0.02 vs −0.17 |

**Findings:**
- **The largest loss bucket is not bad ideas, it is bad stops and bad tides.**
  - In pump_fade, 53% of the losing R was stopped out and then reached its target within a day.
  - The median b-mode trade needed a stop about 2.0R away, roughly 4 ATR; the original sat at 2.1 ATR.
  - Among IS pump_fade trades that did reach T1, the p90 adverse move beforehand was 2.7 ATR.
- **Tide:** level_fade's b-mode losers were shorted while BTC was up a median +4.9% over 24h. When the market squeezes shorts, even a correct level gets overrun.
- **The retail-flip hypothesis is contradicted at 1h on these entries.**
  - SCRIPTS.md proposed waiting for retail to flip back long. But entries where the global L/S *rose* ≥ 0.2 in 24h (a flip in progress) were worse: pump_fade −0.21 vs −0.08, bounce_fade −0.36 vs +0.08, level_fade −0.32 vs −0.14.
  - Entries where retail was still shorting into the pump were slightly *better*.
  - It is **not** registered as a fix; it goes to round 2 as a 72h event study at the +35% crossings where SCRIPTS found it.
- **Funding bleed and pure cost losses are negligible** as their own modes. Cost acts through tight stops: a 4% stop on a $3M coin pays about 0.07R in costs before the market does anything.

### 2. Pre-registered fixes (written before the OOS run; thresholds from the IS diagnosis or prior docs)
Each fix is one change on the base param set. Each (fix × strategy) pair is one trial, and a fix is applied only to the strategies whose IS losses it targets.

| # | fix | exact rule | mechanism (who is forced to trade) | applied to |
|---|---|---|---|---|
| F1 | **tide veto** | No entry if BTC 24h ≥ +2%, BTC 4h ≥ +1%, or the coin's sector (median of the *other* members) 4h ≥ +1% | A market-wide short squeeze: shorts are liquidated in every coin at once, so a single-coin fade is run over | pump_fade, post_pump_decay, level_fade |
| F2 | **stop floor 3 ATR** | stop = max(base stop, entry + 3 × ATR14); target *prices* unchanged; breakeven at 1R of the new risk | Stops just above a pump wick are where the next squeeze fills. 3 ATR ≈ p90 of the adverse move IS winners took before T1 | pump_fade, post_pump_decay |
| F3 | **taker-buy veto** | No entry if the hourly taker buy/sell ratio ≥ 1.1 or the 4h taker imbalance ≥ +0.05 | Aggressive buyers are still lifting offers; taker flow persists (TAPE.md) | pump_fade, level_fade, bounce_fade |
| F4 | **discount only** | Enter only if the perp traded ≤ −1% under its index in the last 24h, the last funding print was ≤ −0.1%, or funding is on a 1h interval | A pump led by spot or the index, with crowded perp shorts paying: the controller exits into the squeeze (SCRIPTS #2: 85% win at the +35% crossing) | pump_fade, post_pump_decay, level_fade |
| F5 | **weekend only** | Enter only Saturday/Sunday UTC (at the signal bar's close) | Thin books and no US/EU desks to absorb or extend the move. Learning #6 and SCRIPTS headline 4 (weekend +8.8% vs weekday −5.1%) | pump_fade, post_pump_decay, level_fade |
| stack | F1 + F2 + F3 + 24h volume ≥ $5M | (for level_fade: F1 + F3 + $5M, no floor, since the sweep-proof stop is already used) | The combination the desk would actually run; the $5M floor is round 0's cost finding | pump_fade, post_pump_decay, level_fade |

**Ledger:** 17 new trials, for a total of **N = 53** in the DSR. The base trials are rerun on the same data so the comparisons are like for like.

**Pass bar:** the same verdict as round 0.
- **SURVIVES:** OOS > 0, IS > 0, beats both placebos, stress R > 0, t_day ≥ 2 and DSR ≥ 0.95.
- **adopt-to-shadow:** OOS > 0, beats the same-hour placebo in both halves, and stress R ≥ 0, but not significant.
- **needs more data:** OOS n < 30.
- **reject:** anything else.

<!-- results below were added AFTER the single OOS run -->

### 3. OOS results: one run, 2026-09-29 (`--round1`, N = 53 trials, V[SR] = 0.0071, so SR\* ≈ 0.19 per trade)

**How to read the table:**
- R is net of fees, slippage and funding. The base rows are round 0's picks, rerun, and they reproduce round 0 exactly.
- **Edge** is the paired edge vs the same-hour placebo: each trade minus its own placebo entries. The t is day-clustered, and the pair after it is IS/OOS.
- **t_day** is on absolute R.
- **Stress** is the whole window at 1.5× fees, 2× slippage and 10% stop overshoot.

| trial | n | coins | IS R | OOS R (n) | edge (t) IS/OOS | stress R | t_day | DSR | verdict |
|---|---|---|---|---|---|---|---|---|---|
| pump_fade (base) | 2211 | 507 | −0.09 | −0.06 (1114) | +0.09 (3.7) +0.10/+0.08 | −0.13 | −2.0 | 0.00 | – |
| post_pump_decay (base) | 2856 | 516 | −0.06 | −0.11 (1435) | +0.07 (3.8) +0.09/+0.04 | −0.13 | −2.9 | 0.00 | – |
| level_fade (base) | 9153 | 527 | −0.16 | −0.18 (4695) | +0.02 (1.7) +0.04/−0.01 | −0.23 | −2.6 | 0.00 | – |
| bounce_fade (base) | 198 | 107 | −0.03 | +0.15 (75) | +0.23 (2.5) +0.21/+0.27 | −0.01 | 0.9 | 0.01 | – |
| pump_fade + F1 tide | 1481 | 452 | −0.03 | −0.05 (745) | +0.10 (3.9) +0.12/+0.08 | −0.09 | −1.7 | 0.00 | reject as strategy; **keep as veto** |
| post_pump_decay + F1 tide | 2173 | 497 | +0.00 | −0.08 (1048) | +0.07 (3.5) +0.08/+0.06 | −0.08 | −2.6 | 0.00 | reject as strategy; keep as veto |
| level_fade + F1 tide | 6260 | 524 | −0.08 | −0.14 (3347) | +0.03 (2.3) +0.06/+0.01 | −0.17 | −3.0 | 0.00 | reject |
| pump_fade + F2 floor | 2206 | 507 | −0.07 | −0.04 (1113) | +0.06 (2.6) +0.07/+0.06 | −0.09 | −1.7 | 0.00 | reject (marginal, edge shrinks) |
| post_pump_decay + F2 floor | 2856 | 516 | −0.05 | −0.10 (1435) | +0.06 (3.7) +0.08/+0.04 | −0.11 | −2.8 | 0.00 | reject |
| pump_fade + F3 taker | 1355 | 455 | −0.02 | −0.05 (688) | +0.10 (2.5) +0.13/+0.06 | −0.08 | −1.1 | 0.00 | reject |
| level_fade + F3 taker | 4486 | 523 | −0.11 | −0.17 (2307) | +0.05 (2.7) +0.08/+0.01 | −0.19 | −2.1 | 0.00 | reject |
| bounce_fade + F3 taker | 145 | 82 | −0.03 | +0.09 (61) | +0.29 (2.4) +0.29/+0.30 | −0.02 | 0.2 | 0.02 | reject (worse than base) |
| **pump_fade + F4 discount** | **133** | **76** | **+0.07** | **+0.17 (69)** | **+0.22 (4.0) +0.18/+0.26** | **+0.10** | **3.2** | **0.54** | **promising: adopt to shadow** |
| post_pump_decay + F4 discount | 179 | 84 | +0.05 | −0.04 (97) | +0.08 (2.5) +0.11/+0.06 | −0.02 | 0.3 | 0.01 | reject |
| level_fade + F4 discount | 229 | 87 | +0.12 | −0.01 (114) | +0.29 (3.4) +0.47/+0.11 | +0.03 | 1.2 | 0.03 | needs more data (relative edge only) |
| pump_fade + F5 weekend | 689 | 373 | +0.03 | −0.07 (358) | +0.07 (1.5) +0.14/+0.00 | −0.08 | −1.3 | 0.00 | reject |
| post_pump_decay + F5 weekend | 1009 | 441 | +0.03 | −0.08 (482) | +0.05 (0.1) +0.08/+0.02 | −0.07 | −1.8 | 0.00 | reject |
| level_fade + F5 weekend | 2973 | 520 | +0.02 | −0.10 (1540) | +0.01 (1.0) +0.01/−0.00 | −0.10 | −1.2 | 0.00 | reject |
| pump_fade + stack | 686 | 319 | +0.03 | −0.02 (367) | +0.08 (2.8) +0.12/+0.06 | −0.02 | −0.8 | 0.00 | reject |
| post_pump_decay + stack | 702 | 323 | +0.02 | −0.03 (351) | +0.09 (4.2) +0.09/+0.10 | −0.02 | −1.0 | 0.00 | reject (relative edge only) |
| level_fade + stack | 1593 | 382 | +0.01 | −0.14 (929) | +0.10 (3.2) +0.19/+0.03 | −0.11 | −1.9 | 0.00 | reject |

#### pump_fade + discount: the one candidate
It is pump_fade entered only when the perp traded at least 1% under its index in the prior 24h, the last funding print was ≤ −0.1%, or funding was on a 1h interval.

**Results:**
- 133 trades on 76 coins.
- R by quarter: +0.14, −0.01, +0.11, +0.25. The same-hour placebo was negative in all four: −0.08, −0.14, −0.15, −0.02.
- Win rate 75% OOS, profit factor 1.65, squeeze rate 3%, max drawdown −4.6R.
- It pays about 0.35% in funding per trade on average, and that is already in the net figure.
- Dropping the 3 best coins (POWR, ACE, PROM) still leaves +0.09R.
- **It misses the multiple-testing bar:** DSR 0.54 against the 0.95 needed. It needs SR 0.20 per trade against SR\* 0.19, on only 69 OOS trades.

**Contamination warning:** the discount hypothesis comes from `docs/SCRIPTS.md` forensics that looked at the *same* 60 days, including the OOS half. This OOS is therefore **not clean** for F4. Only a forward window can confirm it.

#### Verdicts
- **F1 tide veto:** keep it as a desk veto; it is not an edge.
  - It cut losses in both halves for all 3 strategies it was applied to: OOS improved by +0.01 to +0.04R and stress by +0.04 to +0.06R.
  - The same-hour edge is unchanged, as expected: it removes market-beta losses, not bad triggers.
  - No variant turned positive with it, so it is a veto, not a strategy.
- **F2 stop floor:** reject. A wider stop recovered some of the wick-stops, but it also shrank the relative edge (+0.09 → +0.06). Most b-mode trades need a stop about 4 ATR away, and at that width the R targets are too small to pay.
- **F3 taker veto:** reject. It looked strong in IS (pump_fade −0.09 → −0.02), but the OOS gain was ≈ +0.01R, and it hurt bounce_fade.
- **F4 discount:** adopt to shadow for pump_fade only. Reject it for post_pump_decay. level_fade needs more data.
- **F5 weekend:** reject. IS was positive on all 3 strategies (+0.02 to +0.03), and OOS was negative on all 3 (−0.07 to −0.10). This is the clearest overfit in the round: the "weekend fades" belief does not repeat as an hourly entry filter.
- **stack:** reject. It improved IS by +0.12 to +0.17R, but OOS stayed at −0.02 to −0.14.

### 4. What round 1 taught us
1. **Filters mined from IS decay by 50–100% OOS.** Every IS-derived filter lifted IS by 0.05–0.17R and OOS by only 0–0.04R. In a single-regime window, the "precursors" of a loss are mostly the first half's regime.
2. **Losses come from the tide, not the trigger.** The same-hour edge of pump_fade and post_pump_decay stayed at +0.06 to +0.10R (t 3–4) under every fix. The absolute R is negative because random shorts lose about 0.1R in this window. The only lever that changed the *sign* was a different **situation** (discount pumps), not a better **filter**.
3. **"Stopped then worked" (b-mode) is not fixed by a wider stop.** The move exists, but its adverse excursion is as large as the target. A re-entry rule is the thing to test, not a wider stop.
4. **Two forensics hypotheses failed their first test:** weekend-only, and the retail flip (worse at 1h in IS). **One passed its first test:** the discount squeeze fade.

### 5. Next round
1. **Forward-test pump_fade + discount in shadow** (docs/LADDER.md style) on post-09-29 data, and rerun the ledger on the next 30-day window. Promote it only if the fresh OOS is > 0 with n ≥ 50.
2. **Hedged pump family:** book pump_fade / post_pump_decay against a same-hour, beta-weighted short basket's inverse (the time placebo is the hedge P&L). Measure net of hedge costs. The relative edge is the only result that repeats in every round.
3. **Re-entry after a wick-stop:** re-short when price closes back below the stopped level within 3 bars, with a fresh wick-based stop. This targets the 35% b-mode share directly.
4. **Retail flip as an event study** at the +35% crossings (72h horizon, as in SCRIPTS), not as an hourly entry filter.
5. **Discount funding budget:** exit a discount short if the 1h-interval funding it has paid exceeds 0.5R without price follow-through. Test this on the shadow log, not on the backtest.


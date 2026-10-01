# Hedge overlay: shorts + a long BTC / ETH / sector hedge (research 2026-09-30)

Question from the desk: *"if we hedge our shorts with a BTC long, how would it turn out?"* with the caveat that
the desk does not short at random and actively evaluates, so the mechanical backtest is only an approximation.

Code: `src/shortbot/backtest/hedge.py` (hedge-leg math, ratios, stats) and `hedge_report.py` (runs, desk-like
subset, live trades, tables). Tests: `tests/test_hedge.py`. Read-only: hist.db and shortbot.db are opened with
`mode=ro`; nothing touches the exchange, the journal or the guardian.

    .venv/bin/python -m shortbot.backtest.hedge_report --trades DIR/trades.json --results DIR/results.json --out DIR2

## Pre-registration (written before any hedged number was computed)

**Trades.** The selected trial (picked on IS by the backtest, `results.json` → `selected`) of pump_fade,
pump_fade+discount, post_pump_decay, bounce_fade, level_fade, funding_hi and listing_fade (n = 4: shown, not
judged). Primary list: the corrected rerun `bt60_fixed` (matched placebo); `bt60_r2` only for comparison. The
short's own trades are unchanged: the hedge is an overlay.

**Hedge leg** (one per trade, all conventions fixed here):
- Size: long `ratio × the short's INITIAL notional`, held until the short's FINAL exit (partial target exits do
  not shrink it; a desk would not trade the hedge per tranche).
- Entry: a market short enters at the signal bar's close, so the hedge buys the instrument's close of that bar.
  A limit / sell-stop fill happens inside the fill bar at an unknown minute: the hedge buys the bar's (open+close)/2.
- Exit: time / eod / funding-budget exits happen at a bar close, so the hedge sells that close. Stops, targets and
  breakeven fill inside the exit bar at an unknown minute: the hedge sells the bar's (open+close)/2.
- Costs: taker fee (5 bp) on both legs, slippage from the sim's model (`Costs.slip(24h volume)`; BTC/ETH hit the
  1 bp floor), both legs at 1x (a hedge exit is a plain market order, not a stop-market).
- Funding: every real print in hist.db inside the holding window (same rule as the sim: a close-time entry comes
  right after that hour's print, a close-time exit right after the next one), paid on the notional at the print's
  bar open. **A LONG pays positive funding.**
- Everything is reported in the SHORT's R: hedge P&L % of the short's notional / the short's risk %.

**Hedge ratios** (the variants; clipped to [0, 3], a missing estimate falls back to 1.0):
| variant | instrument | ratio |
|---|---|---|
| `none` | – | 0 (the backtest as it was) |
| `btc_1` | BTC perp | 1.0 |
| `btc_ols` | BTC perp | OLS beta of the coin's 1h log returns on BTC's over the 72h ending at the signal bar (the `beta` feature) |
| `btc_shrink` | BTC perp | 0.5 × OLS beta + 0.5 (shrunk 50% toward 1) |
| `btc_mv` | BTC perp | minimum-variance ratio Cov/Var of OVERLAPPING 4h simple returns over the prior 72h (a longer horizon than the 1h OLS, so it is not the same number: alts lag BTC within the hour) |
| `btc_cond` | BTC perp | `btc_shrink`, but only when the tide is up at entry (BTC 24h ≥ 0 or BTC 4h ≥ 0), else no hedge |
| `eth_1` | ETH perp | 1.0 |
| `eth_shrink` | ETH perp | 0.5 × OLS beta on ETH (72h, 1h log returns) + 0.5 |
| `sec_1` | sector basket | 1.0 |
| `sec_shrink` | sector basket | 0.5 × OLS beta on the basket + 0.5 |
| `alt_1` | top-20 alt basket | 1.0 |
| `alt_shrink` | top-20 alt basket | 0.5 × OLS beta on the basket + 0.5 |

**Amendment (lead, still before any hedged number was computed):** every `*_shrink` ratio (and `btc_cond`) is
clamped to [0.5, 1.5] instead of [0, 3]; the top-20 alt basket is added because the relative edge was measured
against random alts: equal weight of the 20 perps with the most quote volume over the 7 days ending at the entry
bar, BTC, ETH, USDC, PAXG, XAUT and the coin excluded, each member paying its own costs and funding.

Sector basket: equal weight of the coin's primary sector (Binance tags, `features.primary_sector`), the coin
excluded, the 10 most liquid members with ≥ $5M 24h volume at entry that have bars at entry and exit; each
member pays its own fees, slippage and funding. Fewer than 3 members: the trade falls back to the BTC leg.

**Per strategy × variant:** n, coins, avg R net, win %, stdev R, Sharpe per trade, max drawdown in R (sequential
by exit time), p5 / p1 R (the squeeze tail), corr(trade R, BTC return over the trade), IS vs OOS avg R (split at
`results.json` `mid`), day-clustered t, variance ratio (var hedged / var unhedged), avg hedge P&L in R and avg
hedge cost in R (fees + slippage + funding).

**Diagnostics (amendment):** mean realized ρ² per trade (corr² of the coin's and the hedge's 1h log returns over
the bars the trade was open, trades with ≥ 4 hours) vs BTC, ETH and the alt basket; the share of stop-outs
(`stop`, `stop_gap`) that were idiosyncratic: residual log move to the stop (coin − shrunk beta × BTC) > 2σ of the
prior-72h hourly residual × √hours held; avg R by BTC's move OVER the trade (up ≥ +1%, down ≤ −1%, chop between;
ex-post, descriptive only); the squeeze tail (short lost ≥ 1R) split by BTC down / up over the trade.

**Desk-like subset** (our discretion, approximated by the live filters):
- Tide veto: drop entries with BTC 24h ≥ +2%, BTC 4h ≥ +1% or sector 4h ≥ +1%.
- Liquidity tier: 24h volume ≥ $5M and OI ≥ $10M.
- Tape stand-aside states (pump in progress, accepted breakout, spot-led rally, attention rising, liquidation
  cascade) need social / sweep / spot-flow inputs that the backtest features do not carry: **not applied** (noted).
- Registry gates: drop retired strategies (level_fade, post_pump_decay, laggard, delisting); bounce_fade is on
  probation, so it is weighted 0.5 in the combined desk book.
- Report per strategy and a combined desk book (pump_fade ∪ pump_fade+discount ∪ bounce_fade ∪ funding_hi,
  deduplicated by coin and entry hour).

**Live trades:** every trade in `data/shortbot.db` (read-only). Hedge in at `opened_at`, out at `closed_at` (an
open trade is marked at the last hist.db close and flagged), prices linearly interpolated inside the 1h bar
(minute fraction of open→close; no 5m BTC in cache). Ratio as-of the last closed hour before entry. R uses
`initial_stop` (else `stop_price`). A trade outside hist.db's window is reported as such.

**Decision rule (fixed now).** A hedge variant "improves risk-adjusted results" for a strategy when the hedged
Sharpe per trade beats the unhedged one in BOTH halves (IS and OOS) and the variance ratio is < 0.9. It is a
money-maker only if hedged avg R > 0 in both halves after hedge costs. **Adopt (lead's rule, amendment):** a hedge
is worth adopting only if the hedged avg R stays > 0 after costs AND its day-clustered t is not lower than the
unhedged one. Tables mark `risk+` / `risk-` for the first rule and `ADOPT` for the second. Anything else is
descriptive.

## Results (run 2026-09-30, window 2026-07-27 → 09-29)

**Data.** Primary trade list: `bt60_fixed`. `bt60_r2` gives **identical** hedge numbers (no cell
differs): the placebo/DSR fix changed the controls, not the selected trials' trades. Context: BTC +27.5% and the
median coin +25.5% over the window, so a long leg earned drift. With the matched placebo, the relative edge is
about 0 for pump_fade, post_pump_decay and level_fade. Only pump_fade+discount (+0.14R, t 3.1) and bounce_fade
(+0.14R, t 1.4) keep one. A hedge can only turn relative edge into absolute P&L, so read the tables with that in mind.

### Per strategy (selected trial; R = the short's R, after all short and hedge costs)

| strategy (n) | variant | avg R | IS / OOS | SR/trade | t_day | max DD R | p1 | var ratio | hedge P&L R | hedge cost R | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| pump_fade (2211) | none | −0.076 | −0.088 / −0.064 | −0.102 | −2.01 | −180 | −1.19 | 1.00 | | | |
| | btc_1 | −0.059 | −0.059 / −0.059 | −0.080 | −2.46 | −136 | −1.33 | 1.00 | +0.017 | −0.025 | risk- |
| | btc_shrink | −0.062 | −0.065 / −0.060 | −0.084 | −2.54 | −143 | −1.33 | 1.00 | +0.014 | −0.024 | risk- |
| | btc_cond | −0.059 | −0.062 / −0.055 | −0.079 | −2.34 | −136 | −1.31 | 1.00 | +0.017 | −0.019 | risk- |
| | eth_1 | −0.043 | −0.024 / −0.061 | −0.056 | −2.40 | −99 | −1.40 | 1.06 | +0.033 | −0.025 | risk- |
| | alt_shrink | −0.066 | −0.077 / −0.055 | −0.089 | −2.91 | −147 | −1.41 | 0.99 | +0.010 | −0.012 | risk- |
| pump_fade+discount (133) | none | **+0.122** | +0.073 / +0.167 | +0.202 | **+3.19** | −4.6 | −1.07 | 1.00 | | | |
| | btc_1 | +0.113 | +0.072 / +0.151 | +0.188 | +3.07 | −4.6 | −1.13 | 1.00 | −0.009 | −0.013 | risk- |
| | btc_shrink | +0.110 | +0.065 / +0.151 | +0.183 | +3.04 | −4.6 | −1.13 | 0.99 | −0.012 | −0.012 | risk- |
| | btc_cond | +0.116 | +0.063 / +0.164 | +0.192 | +3.09 | −4.6 | −1.09 | 1.00 | −0.006 | −0.008 | risk- |
| | eth_1 | +0.110 | +0.068 / +0.148 | +0.183 | +3.06 | −4.5 | −1.11 | 0.99 | −0.012 | −0.013 | risk- |
| | alt_shrink | +0.112 | +0.060 / +0.160 | +0.187 | +3.06 | −4.6 | −1.19 | 1.00 | −0.010 | −0.006 | risk- |
| post_pump_decay (2856) | none | −0.084 | −0.057 / −0.111 | −0.134 | −2.93 | −275 | −1.13 | 1.00 | | | |
| | btc_1 | −0.066 | −0.023 / −0.109 | −0.108 | −3.26 | −214 | −1.22 | 0.95 | +0.018 | −0.020 | risk- |
| | eth_1 | −0.052 | +0.003 / −0.107 | −0.083 | −2.04 | −196 | −1.26 | 1.01 | +0.032 | −0.020 | risk- |
| bounce_fade (198) | none | +0.038 | −0.032 / +0.153 | +0.036 | +0.88 | −9.9 | −1.14 | 1.00 | | | |
| | btc_1 | +0.028 | −0.031 / +0.125 | +0.026 | +0.78 | −10.4 | −1.29 | 0.97 | −0.010 | −0.020 | risk- |
| | btc_shrink | +0.026 | −0.040 / +0.134 | +0.025 | +0.84 | −10.5 | −1.26 | 0.98 | −0.012 | −0.018 | risk- |
| | eth_1 | +0.045 | −0.014 / +0.143 | +0.043 | +0.94 | −10.7 | −1.26 | 0.97 | +0.007 | −0.020 | risk- ADOPT |
| | eth_shrink | +0.047 | −0.019 / +0.155 | +0.044 | +0.95 | −10.9 | −1.24 | 0.98 | +0.008 | −0.016 | risk- ADOPT |
| level_fade (9153) | none | −0.170 | −0.157 / −0.182 | −0.211 | −2.58 | −1668 | −1.10 | 1.00 | | | |
| | btc_1 | −0.114 | −0.052 / −0.173 | −0.150 | −3.12 | −1128 | −1.31 | 0.90 | +0.055 | −0.024 | risk- |
| | btc_shrink | −0.129 | −0.080 / −0.175 | −0.170 | −3.50 | −1255 | −1.30 | 0.88 | +0.041 | −0.023 | risk- |
| | sec_1 | −0.059 | −0.066 / −0.053 | −0.061 | −2.65 | −625 | −1.94 | 1.44 | +0.110 | −0.015 | risk- |
| funding_hi (774) | none | −0.095 | −0.099 / −0.090 | −0.127 | −2.41 | −79 | −1.16 | 1.00 | | | |
| | btc_1 | −0.097 | −0.087 / −0.110 | −0.130 | −1.56 | −78 | −1.50 | 1.02 | −0.003 | −0.020 | risk- |
| | alt_shrink | −0.138 | −0.177 / −0.092 | −0.177 | −3.11 | −108 | −1.82 | 1.11 | −0.044 | +0.001 | risk- |
| listing_fade (4) | | | | | | | | | | | insufficient |

- **Other variants** (`btc_ols`, `btc_mv`, `eth_shrink`, `sec_*`, `alt_1`) sit inside the same ranges. The sector and
  alt baskets are often *worse*: their variance ratio is 1.1–2.4 on funding_hi and level_fade, because a basket of
  alts carries its own pumps.
- **Rules:** no variant passes the risk rule anywhere. "ADOPT" appears only on bounce_fade with ETH: +0.007R and
  t 0.94 vs 0.88, which is noise on 198 trades with t < 1. (It also appears on listing_fade, n = 4, ignored.)
- Full tables with every variant, win %, stdev, p5 and corr are produced by `hedge_report`.

### Why the hedge cannot reduce risk here

| strategy | corr(trade R, BTC over the trade) | realized ρ² (1h, in trade) BTC / ETH / top-20 alts | stop-outs | idiosyncratic stops |
|---|---|---|---|---|
| pump_fade | −0.14 | 0.23 / 0.23 / 0.22 | 752 | 51% |
| pump_fade+discount | −0.06 | 0.14 / 0.12 / 0.18 | 27 | 70% |
| post_pump_decay | −0.21 | 0.21 / 0.22 / 0.21 | 624 | 48% |
| bounce_fade | −0.05 | 0.13 / 0.13 / 0.14 | 78 | 8% |
| level_fade | −0.33 | 0.23 / 0.24 / 0.21 | 3256 | 38% |
| funding_hi | −0.14 | 0.13 / 0.14 / 0.15 | 210 | 28% |
| desk book | −0.05 | 0.16 / 0.16 / 0.17 | 152 | 38% |

- **The market explains little of a trade's R.** Hour to hour, BTC explains 13–23% of a coin's variance. But a trade's
  R is set by where the stop and targets sit (about 7% away) against a BTC move of about 1% over a 6–24h hold.
  corr(R, BTC) is −0.05 to −0.33, so a perfect hedge could remove only ρ² ≈ 0.3–11% of the variance. Measured
  variance ratios are 0.88–1.06.
- **Most stop-outs are the coin's own squeeze.** 38–70% of stop-outs had a residual (coin − β·BTC) beyond 2σ, and a
  BTC long does not pay for those. bounce_fade is the exception: 8% idiosyncratic, so its stops are
  market-driven. Even there, the hedge's cost outweighed what it saved.
- **Where the hedge's P&L came from.** On pump_fade, post_pump_decay and level_fade the hedge's gross P&L is
  positive. That is the BTC +27.5% drift, not risk reduction. The day-clustered t got *worse* in every one of them,
  because the hedge removes day-level noise from a strategy that loses on average.

### By regime (BTC's move OVER the trade, ex-post) and the squeeze tail (avg R, short lost ≥ 1R)

| strategy | BTC over trade | n | none | btc_1 | btc_shrink | alt_shrink |
|---|---|---|---|---|---|---|
| pump_fade | up ≥ +1% | 224 | −0.258 | +0.255 | +0.228 | +0.075 |
| | chop | 1880 | −0.068 | −0.091 | −0.092 | −0.083 |
| | down ≤ −1% | 107 | +0.157 | −0.152 | −0.144 | −0.062 |
| | squeeze + BTC down | 251 | −1.073 | −1.170 | −1.162 | −1.119 |
| | squeeze + BTC up | 372 | −1.076 | −0.932 | −0.942 | −0.932 |
| pump_fade+discount | up / chop / down | 13 / 110 / 10 | +0.33 / +0.10 / +0.11 | +0.42 / +0.09 / +0.01 | +0.40 / +0.08 / +0.01 | +0.35 / +0.09 / +0.07 |
| level_fade | up / chop / down | 2153 / 5394 / 1606 | −0.43 / −0.25 / +0.46 | +0.10 / −0.27 / +0.11 | +0.05 / −0.27 / +0.10 | −0.01 / −0.23 / +0.08 |
| desk book | up | 40 | +0.215 | +0.413 | +0.418 | +0.319 |
| | chop | 385 | −0.046 | −0.070 | −0.071 | −0.087 |
| | down | 40 | +0.217 | +0.036 | +0.044 | −0.002 |
| | squeeze + BTC down | 43 | −1.032 | −1.097 | −1.095 | −1.127 |
| | squeeze + BTC up | 55 | −1.027 | −0.998 | −0.996 | −0.957 |

- **By regime:** the hedge adds +0.2 to +0.5R when BTC rises ≥ 1% during the trade. It gives back 0.1 to 0.35R when
  BTC falls ≥ 1%. In chop, where about 80% of trades live, it costs about 0.02R.
- **Squeeze tail:** 30–45% of squeezes happen with BTC *down*. There the hedge deepens the loss by 0.04 to 0.15R.
  p1 gets worse in every strategy (e.g. pump_fade −1.19 → −1.33).

### Desk-like subset

Filters: tide veto, $5M volume and $10M OI, retired strategies dropped, bounce_fade at 0.5. Tape states are not
applied.

| desk (n) | none | btc_1 | btc_shrink | btc_cond | eth_1 | alt_shrink |
|---|---|---|---|---|---|---|
| pump_fade (288) | −0.011 (t 0.17) | −0.031 | −0.032 | −0.025 | −0.038 | −0.060 |
| pump_fade+discount (14) | +0.009 | −0.002 | −0.003 | +0.004 | −0.005 | +0.027 |
| bounce_fade (73) | +0.129 | +0.106 | +0.109 | +0.114 | +0.116 | +0.103 |
| funding_hi (103) | −0.026 | −0.044 | −0.040 | −0.039 | −0.046 | −0.074 |
| **book (465)** | **−0.001** (IS +0.007 / OOS −0.008, DD −11.7) | −0.019 (DD −17.3) | −0.019 | −0.014 | −0.023 | −0.044 (DD −27.1) |

The tide veto already removes most "BTC up" entries. What is left is mostly chop, where the hedge is pure cost.

### Live trades (data/shortbot.db, read-only; hist.db ends 2026-09-29 16:00 UTC)

| id | coin | setup | short $ (R) | BTC % in trade | btc_1 $ (R) | btc_shrink $ (R) | btc_cond $ (R) | eth_1 $ (R) | alt_shrink $ (R) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | QNT | pump_fade | −0.50 (−0.06) | +0.20 | +0.04 (+0.00) | +0.04 (+0.01) | +0.04 (+0.01) | −0.03 (−0.00) | −0.02 (−0.00) |
| 2 | NEAR | lower_high_fade | −12.34 (−0.32) | +0.12 | +0.00 (+0.00) | +0.00 (+0.00) | +0.00 (+0.00) | +0.09 (+0.00) | +8.41 (+0.22) |
| 3 | SUI | lower_high_fade | −17.85 (−0.39) | +0.15 | +0.33 (+0.01) | +0.49 (+0.01) | +0.49 (+0.01) | −0.28 (−0.01) | +9.89 (+0.22) |
| 4 | QNT | pump_fade | −31.32 (−0.54) | −0.55 | −2.67 (−0.05) | −1.34 (−0.02) | 0 | −2.49 (−0.04) | −1.53 (−0.03) |
| 5 | ONDO | level_fade | +59.34 (+1.89) | −0.43 | −5.39 (−0.17) | −8.09 (−0.26) | 0 | −4.56 (−0.15) | −8.44 (−0.27) |
| 6 | ZEC | bounce_fade | +12.64 (+0.35) | +1.16 | +10.14 (+0.28) | +15.21 (+0.43) | 0 | +12.74 (+0.36) | +5.25 (+0.15) |
| 7 | HBAR | pump_fade | +56.98 (+1.17) | +0.78 | +6.38 (+0.13) | +9.56 (+0.20) | 0 | +11.26 (+0.23) | +13.05 (+0.27) |
| 8 | ZRO | pump_fade | −0.67 (−0.02) | −0.06 | −1.73 (−0.04) | −2.60 (−0.06) | −2.60 (−0.06) | −1.96 (−0.05) | −1.58 (−0.04) |
| 9 | CRV (open) | pump_fade | +40.35 (+1.09)* | −0.97 | −9.09 (−0.25) | −13.63 (−0.37) | −13.63 (−0.37) | −13.88 (−0.37) | −13.63 (−0.37) |
| **total** | | | **+106.63 (+3.17)** | | **−1.99 (−0.08)** | **−0.36 (−0.07)** | −15.70 (−0.41) | +0.89 (−0.03) | +11.40 (+0.15) |

\* CRV is still open. Its hedge is marked at the last hist.db close, 1.3h after entry. Its short P&L is the
journal's value.

Notes on the live trades:
- They are the desk's demo account. The hedge uses mainnet prices from hist.db.
- Trade 1 has no `initial_stop`, so `stop_price` is used.
- The alt basket's +$8–10 on NEAR and SUI (09-27) came mostly from QNT (+8.6%), which was pumping *inside* the
  basket. That is luck, not hedging.
- On 9 trades, the hedges add between −$16 and +$11, against +$107 on the shorts. It is noise, and the BTC ones net
  slightly negative.

## Verdict

1. **Does a BTC long improve risk-adjusted results? No.**
   - No hedge variant passes the pre-registered risk rule in any strategy.
   - The only nominal "ADOPT" (bounce_fade + ETH, +0.007R, t 0.94) is noise.
   - In the one strategy with a real edge (pump_fade+discount), every hedge lowers avg R (−0.006 to −0.012) and
     t (3.19 → 3.04–3.09).
   - In the desk-like book, every hedge turns −0.001R into −0.014 to −0.064R, with a deeper drawdown.
2. **Why:**
   - Our shorts lose to the coin's own squeeze (38–70% of stop-outs are idiosyncratic), not to the market.
   - corr(trade R, BTC) is only −0.05 to −0.33.
   - A 1× BTC leg against a stop about 7% away adds ±0.1–0.3R of BTC noise per trade and removes almost none of
     the variance (ratio 0.88–1.06).
   - In squeezes that come with BTC down (30–45% of them), it makes the loss worse.
3. **Where it "worked":**
   - On the already-negative strategies (pump_fade, post_pump_decay, level_fade) it added +0.02 to +0.06R.
   - That is the BTC +27.5% drift of this window, not protection, and their t got worse.
   - Regime-wise, it pays only when BTC rallies ≥ 1% *during* the trade, which the tide veto already keeps us
     out of at entry.
4. **Cost:**
   - About 0.012–0.025R per trade for a BTC/ETH leg (2 × 5 bp taker + 1 bp slippage + funding paid by the long) at
     the backtest's ~7% average stop.
   - Cost scales with 1/stop distance, so at live stops of 3–5% it is about 0.03–0.05R.
   - Alt and sector baskets are cheaper in fees but noisier.
5. **Recommendation:**
   - **Do not hedge individual shorts with BTC, ETH or a basket.** Keep the tide veto as the market-risk control:
     it avoids the "BTC up" regime at no carrying cost.
   - One idea is untested here: a book-level hedge of net short beta when three or more shorts are open during a
     BTC up-impulse. It would need its own pre-registered test.
   - The desk's discretion does not change this conclusion. The obstacle is structural (idiosyncratic squeeze
     risk ≫ market risk at our stop distances), not a property of random entries.

**Limitations:**
- Hourly bars: intrabar exits use the bar's (open + close) / 2.
- The hedge is not scaled down on partial target exits.
- The results cover one 60-day, alt-up regime.
- The top-20 alt basket (ranked by 7-day volume) contains pumpers.
- Tape stand-aside states are not applied in the desk subset.

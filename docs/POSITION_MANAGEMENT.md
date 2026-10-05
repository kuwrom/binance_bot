# Position management and sizing: the rules the code encodes

Evidence labels: **[WS]** well-supported (peer-reviewed or mathematical), **[MX]** mixed, **[PL]** practitioner lore. Research date: 2026-09-28.

## Core idea
Manage every trade **from its current state**, not from the entry. At each review, ask whether you'd open this trade now, at this price, with this stop. The disposition effect is real and costly: investors realize gains 1.5× more readily than losses (PGR 14.8% vs PLR 9.8%), and the winners they sold beat the losers they held by 3.4% the next year (Odean 1998; Shefrin & Statman 1985) [WS].

## (a) Risk budget: `shortbot.risk_budget`
`risk_$ = equity × base × m_dd × m_vol × m_regime × m_corr`, then `size = risk_$ / |stop − entry|`. Stop-based sizing already scales each trade to its own volatility.

**base.** `risk_per_trade_pct`.
- The `.env` baseline is 1% (config.py); demo experiments move it within the human's bounds [PL; Turtles used 1%/unit].
- Once there are 50 or more trades per setup, cap it with ½ Kelly: `f* = W − (1−W)/b`.
- Half Kelly gives a 1/8 chance of ever halving the account (full Kelly: 1/2) and keeps ¾ of the growth rate (Thorp) [WS].
- Never use raw optimal-f (Vince), which can imply drawdowns of about 97% [WS].

**m_dd = clip(1 − DD/DD_max, 0.25, 1).**
- DD_max defaults to 20% and only scales size; the governor already stops new entries at dd_stop 12% (RISK_GOVERNANCE.md). Based on Grossman–Zhou 1993 [WS theory].
- Never size up after losses.
- Continuous scaling, not an equity-curve on/off switch [MX].

**m_vol = clip(σ_target / σ_BTC_now, 0.5, 1.0).**
- σ_target is the 1-year median of BTC 30-day realized vol.
- Volatility targeting shrinks left tails (Harvey et al. 2018) [WS].
- Never above 1: the out-of-sample benefit of vol timing is weak (Cederburg 2020) [MX].

**m_regime.**
- favorable: 1.0
- caution: 0.75
- hostile or unknown: 0.5 [PL].

**Heat caps.**
- Total open risk ≤ `max_portfolio_heat_pct`.
- Correlated alt shorts count as one cluster, capped at ≤ `max_cluster_heat_pct`.
- The 2nd and later shorts in a cluster get m_corr = 0.5 [PL; Turtle unit caps].

## (b) Zero-based review: `shortbot reassess SYM`
From the current price, compute `stop_now` (the structural invalidation), `target_now`, `EV_now` and `size_ideal`. Actions, first match wins:

1. **Exit** if the thesis is invalid or you wouldn't open it now (EV_now ≤ 0). Don't wait for the stop.
2. **Reduce** to `size_ideal` if the current size is larger (volatility or the budget changed).
3. **Widen with size cut**, at most once per trade, when all three hold [PL; fixed-risk math]:
   - the new stop sits beyond a structural or sweep level;
   - size is cut so the $ loss at the new stop ≤ the initial 1R;
   - the stop hasn't been widened before on this trade.
4. **Tighten / trail.**
   - Chandelier stop for shorts: `lowest_low(22) + 3·ATR(22)` (LeBeau).
   - Move to breakeven only after MFE passes the setup's measured threshold.
   - Stops add value when returns have momentum (Kaminski & Lo 2014) [WS].
5. **Add (pyramid)** only on open profit.
   - After each add, move the stop so the total worst-case loss stays ≤ 1R.
   - Never average down [WS ruin math].
6. **Partials.** Take them only for setups with capped MFE, which includes our fades. Trend-type shorts keep runners [MX].
7. **Time.**
   - Time stop = the 75th percentile of time-to-MFE.
   - Exit if MFE < 0.5R by half that time.
   - Shorten the hold when funding turns negative and `|funding| × remaining intervals > 0.1R`.

## (c) Limit entries: `shortbot propose` (entry groups)
**Levels.** Round numbers and prior highs, where take-profit orders cluster and stops sit just beyond (Osler 2003/2005 FX [WS]; crypto clustering in Urquhart 2017 and FRL 2022 [MX]).
- Rest the sell limit **a few ticks below** a round number or prior high,
- or **above** the stop/liquidation cluster, so it fills after the sweep.
- Put the stop beyond the sweep extreme plus a buffer, never exactly at a round number.
- Coinglass heatmaps are modeled estimates, so use them only as a hint.

**Adverse selection.** Limit orders fill more often when you're wrong (Handa & Schwartz 1996; Linnainmaa 2010; Albers et al. 2025 on the Binance BTC perp) [WS]. Assume filled trades win less than the signal until markouts prove otherwise.

**Cancel when:**
- price moves toward the order faster than ~1·ATR(1h) in 15 minutes, or OI/volume surges (a cascade, not a tap);
- the thesis is invalidated;
- the TTL passes.

**Replace or chase.** Replace an order at most once. Don't chase: a market entry only if EV_now is still positive after taker fees.

## (d) What we measure
- **Per trade, in R:** result, MAE, MFE, time-to-MFE, fees, funding, slippage.
- **Per setup:** E[R], SQN = E[R]/σ_R·√min(N,100), shrunk Kelly, MAE of winners (for stop width), MFE quantiles (for targets).
- **Execution:**
  - fill rate by level type and distance
  - markouts at 1m/5m/1h/24h after the fill
  - **counterfactual outcome of unfilled orders** (the true non-execution cost).
- **Management audit:** "hold the original plan" vs each action taken. Re-fit the multipliers only after 50 or more trades per setup.

Sources: Thorp (Kelly), Grossman & Zhou 1993, Moreira & Muir 2017, Cederburg et al. 2020, Harvey et al. 2018, Man AHL on crypto vol, Odean 1998, Kaminski & Lo 2014, Osler 2003/2005, Urquhart 2017, FRL 2022, Linnainmaa 2010, Handa & Schwartz 1996, Albers et al. 2025 (arXiv 2502.18625), Turtle rules, LeBeau chandelier, Sweeney (MAE), Tharp (SQN).

# Where the edge is, and how to combine signals (research 2026-09-28)

> **Update 2026-10-01:** [INDUSTRY.md](INDUSTRY.md) §6 corrects four points below with new evidence: carry moved (dispersion, TradFi perps, and a crash-gate signal for us), specialise by mechanism, not direction, venue mechanics drive tail losses, and LLM judgement is not the edge (that last point is superseded: since 2026-10-04 the AI's judgement is the measured edge, docs/QUANT_METHODS.md "Design principle"; the rules in force are docs/DOCTRINE.md). The full strategy catalogue is in [strategies/](strategies/README.md).

## How independent (non-market-maker) quants make money
Profitable independents tie every strategy to a **forced or uneconomic flow**, specialise, keep costs low, size small and fixed, and kill strategies when they decay. Most retail traders lose: in Taiwan only about 5% of day traders are profitable in a given year and under 1% persistently, and costs roughly triple the losses (Barber-Lee-Liu-Odean, peer-reviewed).

| Edge family | Status for us |
|---|---|
| **Supply shocks** (team/cliff unlocks: about 90% negative, team unlocks about −25%, pressure starting ~30d before; post-TGE/high-FDV listings: 88% of 2024 airdrops fell; delistings) | **#1 priority.** Best evidence, and a capacity edge (too small for big funds). Keyrock unlock/airdrop studies are practitioner research. **Update 2026-10-12:** our own data shows no edge for pre-unlock shorts (#44, #96; tide_study): unlocks are a half-size `exp_` until a forward test is positive (docs/DOCTRINE.md). Listings keep their place. |
| **Funding-aware fades of overextended alts** | **#2.** Corrected by our data (#38): top-quintile aggregate carry meant trend-on, alt shorts lost; a coin's own funding is never a short trigger, and negative funding is a size flag, not a veto (#22). Rising-OI squeezes stay the main risk (#52). |
| **Beta-hedged cross-sectional short leg** (weak momentum, high dilution) with a BTC trend (TSMOM) regime switch | **#3.** Peer-reviewed factor support (Liu-Tsyvinski-Wu JF 2022). Trend is the most robust crypto premium (Sharpe 0.8–1.5 in working papers). |
| Liquidation-cascade fades | Secondary timing overlay only. There is no reliable early warning, and ADL can close winning shorts (Hyperliquid, $45–52M). |
| Cash-and-carry / funding arbitrage | **Closed.** It decayed from about 30%/yr (2021) to about 4% (2025), below the cash rate. |
| Market making, rebates, latency, cross-exchange arbitrage | **Closed** to us. |

Plan for a **net Sharpe of 0.5–1.0**. Haircut any backtest Sharpe by 50% or more. Costs decide everything: one audit found positive IC but negative net Sharpe after costs on BTC perps. Prefer maker entries (the limit groups).

## Combining many signals into one probability
- **Breadth is mostly illusory.** Twenty alts at ρ ≈ 0.6 give about 1.6 independent bets. K signals with average correlation ρ top out at IC/√ρ, and nearly all of the gain comes from roughly the first 8–10 low-correlation signals. Of 94 equity characteristics, only about 12 were independent (Green-Hand-Zhang RFS 2017).
- **Naive Bayes double-counts correlated signals** (funding, OI and basis are one story) and is badly calibrated. Group signals into families. Logistic regression prices the correlation and is already well calibrated.
- **Small data** calls for 10–20 events per parameter. 200 labels at a 35% hit rate support only 4–7 parameters, not 40. A new family needs t > 3 (Harvey-Liu-Zhu). Keep a **trials register** so deflated Sharpe / SPA corrections are possible.
- **Simple averages beat fitted weights out of sample** (DeMiguel 1/N; Rapach forecast combination). Shrink any fitted weights toward equal.

**Architecture:**
1. Raw signals become robust z-scores.
2. These are grouped into **families**, each with its sign fixed in advance:
   - positioning (funding, OI change, basis)
   - flow (taker imbalance, CVD divergence)
   - stretch (RSI, VWAP stretch)
   - forced flow (sweeps, liquidations)
   - lifecycle (listing age, delisting, unlock)
   - regime
3. Each family score is the mean of its members; the family scores combine into one score.
4. The score is calibrated to P(win) and E[R], minus costs and funding, which gives the trade gate.

**Staged plan:**
- **0–200 labels:** equal-weight families. Map the score to probability with Beta-prior buckets or a one-feature Platt fit. Use p for ranking only. Log every trial.
- **200–1,000 labels:**
  - L2/elastic-net logistic regression on ≤ 8 family scores, shrunk toward equal weight, with a random intercept per setup.
  - Validate with purged/embargoed walk-forward CV grouped by date, then Platt calibration.
  - Adopt only if it beats equal weight on a day-block bootstrap, with calibration slope 0.8–1.2.
- **1,000+ labels:**
  - Challenger: shallow monotone gradient boosting, stacked with the logistic model.
  - Validate with CPCV plus deflated Sharpe > 0.95.
  - Run 4–8 weeks in shadow before any size increase.

**Monitoring:**
- Rolling IC per family.
- Calibration slope alarm outside 0.7–1.3.
- CUSUM on the Brier score.
- Retire a family whose rolling-IC t-stat is below 0 for 2 windows.

Key sources: BIS WP 1087 · Keyrock unlocks & airdrops · Liu-Tsyvinski-Wu JF 2022 · He-Manela-Ross-von Wachter (arXiv 2212.06888) · Barber-Lee-Liu-Odean · Grinold & Kahn · Kakushadze & Yu 2017 · DeMiguel-Garlappi-Uppal 2009 · Rapach-Strauss-Zhou 2010 · Niculescu-Mizil & Caruana 2005 · Harvey-Liu-Zhu 2016 · Green-Hand-Zhang 2017 · McLean & Pontiff 2016 · López de Prado AFML.

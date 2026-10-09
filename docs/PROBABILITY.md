# Level-to-level probability engine: research verdict and design (2026-09-28)

## Verdict
**Sound only as a measurement and calibration layer (meta-labeling), not as an alpha source.**
- **No drift, no edge.** If price is a martingale, the optional stopping theorem gives P(target before stop) = stop/(stop+target) exactly. So EV ≈ 0 before costs, whatever the R:R. Simulated with Student-t GARCH paths: 0.332 / 0.503 / 0.206 vs theory 0.333 / 0.500 / 0.200. Fat tails add no edge; they add stop slippage.
- **Realistic edges move p by a few points.** For a 5% stop / 10% target at 6% daily vol, moving p from 33% to 36% needs about 0.29%/day of drift (Sharpe ≈ 0.9). Separating 0.36 from 0.333 at 2 SE needs about 1,300 independent outcomes; 0.40 needs about 210. Correlated alt shorts count as far fewer independent bets.
- **Crypto short-horizon predictability is weak and cost-fragile.** Liu & Tsyvinski RFS 2021; hourly BTC sign strategies fail at 10bp costs (WP 2026).
- **Kelly with a wrong p is dangerous.** Believing 0.40 when the truth is 0.36 means betting 2.5× Kelly, which drives growth below zero. Use shrunk fractional Kelly under parameter uncertainty (Baker & McHale 2013; MacLean-Thorp-Ziemba 2010).
- **Meta-labeling evidence is thin.** It comes mostly from its promoters (Joubert 2022; Meyer-Barziy-Joubert 2023: calibration helps fixed sizing functions). There is no independent crypto replication.

## Do
1. **Zero-drift baseline calculator**, labeled "no-edge baseline": touch, race and three-way outcomes. Used for fill and touch estimates and sanity checks.
2. **Log triple-barrier outcomes** for every scan candidate and trade, with MFE/MAE, fees and funding, using 1m high/low on the same price series the stop triggers on (mark vs last).
3. **Expectancy tables** by setup × regime × tape state, with confidence intervals. These measure the conditional drift directly.
4. **Shadow mode:** log the model's p next to the analyst's p, and score both with Brier.
5. **Conditioned model**, only after about 300 trade labels: L2 logistic regression on logit(baseline p) plus 3–6 signals, with purged/embargoed CV. It is adopted only if the Brier skill score vs the baseline has a bootstrap CI above 0.

## Don't
- Rank ideas by geometric EV.
- Feed raw model p into Kelly.
- Re-estimate p on every tick and let it override the trade plan.
- Fit per-coin models on dozens of trades.
- Treat alt shorts as independent bets.

## Sizing gate (all of these)
- At least 200–300 out-of-sample, cost-inclusive outcomes under purged walk-forward.
- Brier skill score > 0 vs baseline, significant by bootstrap.
- Reliability slope 0.8–1.2 with confidence bands covering the diagonal.
- Stable across 2 or more regimes.

Then size at 0.25–0.5 Kelly, shrunk further for uncertainty, under hard caps. Below the gate: fixed-fractional risk, with p used for ranking and filtering only.

## Methods (log-price x = ln P/P0, X_t = νt + σW_t, ν = μ − σ²/2; default ν = 0)
**(a) Touch up within T:**
Φ((−u+νT)/(σ√T)) + e^{2νu/σ²}·Φ((−u−νT)/(σ√T)). With ν = 0 this is 2Φ(−u/(σ√T)).

**(b) Up before down, no time limit:**
(1 − e^{−kℓ})/(e^{−ku} − e^{−kℓ}) with k = 2ν/σ². With ν = 0 this is ln(P0/L)/ln(U/L).

**(c) Three-way by T** (Cox & Miller eigen-series, N = 200–400 terms). Verified against Monte Carlo: (0.1655, 0.3442, 0.4902) vs (0.1666, 0.3450, 0.4884).

**Discrete monitoring.** Close-only checks miss touches: −23% on 15m closes, −38% on 1h. Use the BGK shift U′ = U·e^{0.5826σ√Δt}. In Monte Carlo, use the Brownian-bridge crossing probability exp(−2(u−x_i)(u−x_{i+1})/(σ²Δt)).

**Volatility.** EWMA on 5m returns (half-life ≈ 1 day) plus time-of-day seasonality, cross-checked with Parkinson (ln H/L)²/(4 ln 2). HAR-RV beats GARCH intraday for BTC. The vol forecast matters more than the tail shape.

**Fat tails.** Filtered historical simulation:
1. standardise returns by EWMA vol;
2. stationary-block-bootstrap the residuals (Politis-Romano; Politis-White block length);
3. rescale by current vol;
4. run 10k paths.

Cost is about 0.17 s per symbol.

**Limit fills.**
- P(touch) at the limit, plus a trade at least 1 tick through (queue).
- Adverse selection: P(target | filled) < P(target | market). Label from the fill time.
- First-passage fill proxies are poor (Lo-MacKinlay-Zhang 2002), so calibrate on our own fills.

**Calibration.**
- Brier = REL − RES + UNC (Murphy).
- Recalibrate with Platt below about 1000 samples, isotonic above (Niculescu-Mizil & Caruana).
- Buckets need about 100–400 effective samples each.

**Implied volatility** is free only for majors (Binance options: BTC ETH BNB SOL XRP DOGE; Deribit: + AVAX HYPE TRX). Risk-neutral probabilities overstate touches (variance risk premium), so use them only as a cross-check and the BTC IV/RV ratio as a regime input.

Sources: López de Prado AFML (2018) and "10 Reasons Most ML Funds Fail" (JPM 2018); Joubert JFDS 2022; Meyer-Barziy-Joubert JFDS 2023; Perelló et al. PRE 2011; Cont (vol clustering); Liu & Tsyvinski RFS 2021; Baker & McHale Decision Analysis 2013; MacLean-Thorp-Ziemba QF 2010; Shreve SCII §7.2; Cox & Miller 1965; Broadie-Glasserman-Kou 1997; Glasserman 2004; Barone-Adesi et al. 1999; Politis-Romano 1994; Corsi 2009; Niculescu-Mizil & Caruana 2005; Murphy 1973; Lo-MacKinlay-Zhang JFE 2002; Gibbs & Candès NeurIPS 2021.

## Implementation (src/shortbot/probability/)
- **`baseline.py`:** closed forms (touch, race, three-way, BGK shift). `short_geometry` uses a PRICE martingale (ν = −σ²/2). A zero LOG drift biases price upward (Jensen) and would fake an edge. It and `simulate_short` take `side` ('short' default, 'long': the mirrored bracket).
- **`montecarlo.py`:** filtered historical simulation.
  - **Bootstrap:** i.i.d. by default (`mean_block=1`). The block bootstrap replayed QNT's post-blow-off mean reversion and produced a fake +0.09R, so it is not a baseline.
  - **Drift correction:** exact, from the empirical residuals.
  - **Exit fills:** at the end of the crossing step, so gap overshoot = stop slippage.
  - **Validation:** EV_R ≈ 0 ± SE; on QNT, −0.02 ± 0.015R, with stop fills averaging 1.13R.
- **`engine.py`:** `ProbabilityEngine.short_bracket` returns closed form + MC + fill odds and logs predictions (shadow mode).
- **`labels.py`:** triple-barrier labels on 1m MARK candles; the guardian labels due predictions hourly.
- **`evaluate.py`:** Brier + Murphy decomposition, reliability diagram with Wilson CIs, analyst-vs-baseline skill, and expectancy/edge by setup, tape state, regime and source.
- **Where it runs:**
  - every scan logs a vol-scaled 24h research bracket (+1σ√T / −2σ√T) per evaluated coin
  - `reassess` shows `probability_from_here` and the `next_level` exit check (below)
  - limit entries log fill odds
  - CLI `shortbot prob|calibration`; MCP `probability`, `calibration_report`.

## Next-level exit check (src/shortbot/exit_odds.py)
For an open position: P(next unhit target first), P(stop first), P(neither) within the plan's remaining time, the
reward and give-back in initial R, and the EV net of the exit fee and the funding expected until the exit (docs/
POSITION_MANAGEMENT.md "Next-level check"). It is this zero-drift baseline (geometry plus costs), **not an edge
estimate**: under it, holding is worth minus the costs, so it judges exit risk-reward (how much is left to win vs how
much the stop gives back, and what the carry costs), never entries. `reassess` shows it with the `protect_profit` /
`funding_drag` advice; the ladder logs it in `ladder_shadow.next_level`. Nothing acts on it automatically until it
passes the sizing gate above on labelled outcomes.

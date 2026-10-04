# Professional quant methods: what we have, what we're missing (2026-10-04)
A gap analysis against the open-source state of the art, by stage of the research-to-production workflow. "Have" points at our code; "Learn from" points at an open implementation.

**Sources read (primary):**
- [awesome-quant](https://github.com/wilsonfreitas/awesome-quant) (the curated index).
- Jansen, [Machine Learning for Trading, 3rd ed. code](https://github.com/stefan-jansen/machine-learning-for-trading) (27 chapters, research to production).
- Microsoft [Qlib](https://github.com/microsoft/qlib) and [RD-Agent](https://github.com/microsoft/RD-Agent).
- Carver's [pysystemtrade](https://github.com/pst-group/pysystemtrade) (backtesting guide).
- Hudson & Thames [mlfinlab](https://github.com/hudson-and-thames/mlfinlab) (module list; the library is now commercial) and [ArbitrageLab](https://github.com/hudson-and-thames/arbitragelab).
- Portfolio and statistics: [skfolio](https://github.com/skfolio/skfolio), [Riskfolio-Lib](https://github.com/dcajasn/Riskfolio-Lib), [arch](https://github.com/bashtage/arch), [hmmlearn](https://github.com/hmmlearn/hmmlearn).
- Engines and tooling: [NautilusTrader](https://github.com/nautechsystems/nautilus_trader), [Hummingbot](https://github.com/hummingbot/hummingbot), [FreqAI](https://github.com/freqtrade/freqtrade/blob/develop/docs/freqai.md), [vectorbt](https://github.com/polakowo/vectorbt), [alphalens](https://github.com/quantopian/alphalens).
- Bailey, Borwein, López de Prado and Zhu, [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) (2015).

No single repo has it all. Jansen's ML4T is the best single map of the workflow. pysystemtrade is the best on sizing. skfolio and arch are the best libraries for portfolio statistics and validation.

## The gap matrix
Status: ✅ have · ◐ partial · ✗ missing. Fit: how much it would help this desk, given what our studies found.

### 1. Research design and labels
| method | what it is | status | fit |
|---|---|---|---|
| Pre-registration, cost model, run logging | rules, costs and universe fixed before a run; every run stamped | ✅ `provenance.py`, module docstrings | — |
| Triple-barrier labels | an outcome = whichever of target, stop or time comes first | ✅ `probability/labels.py` | — |
| **Meta-labeling** | a second model on the primary signal's events decides take/skip and size (López de Prado) | ✗ | **High.** Our pump shorts win at the median but lose in the tail (BACKTEST.md chains). A skip/size model on those events is exactly that problem |
| Trend-scanning labels | the label is the sign of the most significant trend over several horizons | ✗ | Medium |
| Sample uniqueness weights | overlapping events weighted by how unique their outcome window is | ◐ We cluster the standard errors but don't weight the samples | Medium |

### 2. Features
| method | status | fit |
|---|---|---|
| Momentum, reversal, volatility, liquidity, multi-timeframe states | ✅ signals/, `mtf.py` | — |
| **Microstructure features**: VPIN, Kyle's lambda, Roll spread, Amihud illiquidity (mlfinlab "useful financial features"; ML4T ch. 3 and 8) | ◐ We have taker imbalance, CVD and depth, not the classic estimators | **High.** Crypto pumps are flow events, and these come straight from the 1m/5m taker volume we now stream |
| **Model-based features**: GARCH/HAR volatility, Kalman filters, HMM regime probabilities (ML4T ch. 9) | ✗ We use EWMA and Parkinson σ only | **High** for HAR/GARCH σ (stops and sizing); **High** for HMM regimes (see §4) |
| Fractional differentiation (stationary but memory-preserving prices) | ✗ | Low until we fit ML models |
| Dollar/volume bars instead of time bars (ML4T ch. 3: dollar bars have the most normal returns) | ✗ | Low-medium |
| Text / NLP features | ◐ news and social as untrusted data, no embeddings | Low |

### 3. Models
| method | status | fit |
|---|---|---|
| Linear baseline (ridge / LASSO / elastic net) | ◐ ridge in `xs_factors.py` (CTREND) | — |
| Gradient boosting (LightGBM / XGBoost) with SHAP | ✗ | Medium, and only after meta-labeling defines the task. ML4T: boosting is the strong tabular baseline |
| Deep learning, transformers, RL | ✗ | Low: our data volume and the evidence (ML4T ch. 13's "when depth helps") don't justify it |
| Latent factors (PCA eigenportfolios), causal ML | ✗ | Low |

### 4. States, regimes and persistence
| method | status | fit |
|---|---|---|
| Rule-based state labels (tape, lifecycle, regime, gates) | ✅ | — |
| **Markov transition matrices** on our states (P(state at t+1 given state at t), persistence, expected durations) | ✗ The states are logged (candidates, `mtf.db`) but nothing estimates transitions | **High.** "Post-pump decay → pump resumes" is the squeeze tail that kills our shorts' mean |
| **Hidden Markov / Markov-switching regimes** (hmmlearn, statsmodels) | ✗ | **High** for the BTC/market regime: probabilities instead of a hard gate, with persistence |
| Structural-break tests (CUSUM, SADF: bubble detection) | ✗ | Medium: SADF is a bubble test, and pumps are mini-bubbles |

### 5. Validation and overfitting control
| method | status | fit |
|---|---|---|
| Deflated Sharpe, effective N, trial register | ✅ `trial_register.py`, `trials_eff.py` | — |
| Clustered t, block bootstrap, matched placebos | ✅ | — |
| Walk-forward | ◐ a single in-sample/out-of-sample split, a weekly refresh, weekly CTREND refits, forward tests | — |
| **Probability of Backtest Overfitting (PBO) via CSCV** | ✗ | **High and cheap.** The register already holds per-trial daily series. PBO answers "how often is the in-sample winner below median out of sample?" across all ~290 effective trials |
| **Combinatorial purged CV (CPCV)** with embargo | ✗ (one simple purge in `analogs.py`) | High once any model is fitted |
| **Reality Check / SPA, StepM, Model Confidence Set** (arch) | ✗ | Medium-high: a formal "is the best strategy better than the benchmark" across all trials, complementing DSR |
| Benjamini-Hochberg FDR (ML4T ch. 7) | ✗ | Medium: controls false discoveries across many card tests |
| Minimum backtest length | ✗ | Medium: tells us how many days a claimed Sharpe needs |

### 6. Sizing and portfolio construction
| method | status | fit |
|---|---|---|
| Per-trade risk budget, governor, liquidity tiers, joint-Kelly caution | ✅ `risk_budget.py`, `governor.py`, `book.py` | — |
| **Volatility targeting + forecast scaling** (Carver: scale forecasts to a common size, cap them, size positions to an annual vol target, buffer to cut turnover) | ✗ We size by stop distance and R, not by a continuous forecast | **High** for the long sleeve (weekly L1/L2) and for combining several weak signals |
| **Forecast combination with a diversification multiplier** (pysystemtrade "forecast combine") | ✗ Signals add points; no correlation-aware combination | High |
| HRP / risk budgeting / CVaR across positions (skfolio, Riskfolio) | ✗ A correlation multiplier only | Medium: few concurrent positions today, more once longs run beside shorts |
| Covariance shrinkage (Ledoit-Wolf, denoising) | ✗ | Medium (needed by the above) |
| Conformal position sizing (ML4T ch. 17) | ✗ | Low-medium |

### 7. Costs and execution
| method | status | fit |
|---|---|---|
| Fees, slippage, funding in every result; stress costs | ✅ | — |
| Live fill quality vs plan | ◐ `fill_quality.py`, `fill_report.py` | — |
| **Transaction-cost analysis**: implementation shortfall, spread and market-impact calibration from our own fills | ◐ | Medium-high: our edges are thin, so measured impact decides survival |
| Execution algorithms (TWAP, VWAP, Almgren-Chriss) | ✗ | Low at our size |
| Market making (Avellaneda-Stoikov; Hummingbot) | ✗ | Low: latency game, a different business |

### 8. Risk and production
| method | status | fit |
|---|---|---|
| Kill switch, drawdown ladder, heartbeat, stale-process checks | ✅ | — |
| VaR/CVaR, stress tests, factor/sector risk decomposition (ML4T ch. 19) | ◐ sector map; no VaR/CVaR of the book | Medium |
| **Drift detection / alpha-decay monitoring** (ML4T ch. 26: separate pipeline divergence from performance decay) | ◐ The setup ladder brakes on drawdown; the replay checks pipeline fidelity; no feature-drift monitor | Medium |
| Research-to-live parity (same code in both) | ✅ the replay (#29), `mtf.py` | — |

### 9. AI research agents
| method | status | fit |
|---|---|---|
| An LLM-driven research loop: propose a factor/model, implement, evaluate, keep or discard (RD-Agent(Q), NeurIPS 2025; its returns claims are the authors' own) | ◐ That is what this desk does, by hand-run sessions | Medium: RD-Agent's evaluate-and-archive loop is a pattern to borrow, with our trial register as the evaluator |

## What to adopt, in order (first pass; superseded by "Updated adoption order" below)
Ordered by fit to what our own studies found, and by cost:
1. **PBO via CSCV, plus SPA/StepM** on the trial register. It reuses the daily series already stored, and turns "nothing survives" into a measured overfitting probability. Implementation to learn from: arch's `SPA` / `StepM` / `MCS`; CSCV is in the paper above.
2. **Transition matrices on our states, then an HMM market regime.** Estimate P(re-pump | post-pump state, timeframe states) from candidates and `mtf.db`. It attacks the short book's tail directly.
3. **Meta-labeling on the pump-fade events**: a take/skip and size model over the primary rule, with CPCV and sample-uniqueness weights. Learn from Jansen ch. 7/11/12 and AFML (mlfinlab is paid; the open notebooks in "Machine-Learning-for-Asset-Managers" cover parts).
4. **HAR / GARCH volatility forecasts** (arch) for stop width and sizing, replacing EWMA σ where it tests better.
5. **Carver-style vol targeting, forecast scaling and combination** for the long sleeve and for multi-signal setups (pysystemtrade's forecast-scale, forecast-combine and position-scaling stages).
6. **Microstructure features** (VPIN, Kyle's lambda, Amihud) from the streamed 1m/5m taker volume, tested as squeeze predictors.
7. **TCA from our own fills** (implementation shortfall by setup), once demo/live fills accumulate.

**Not for now:** deep learning, RL, options and volatility surfaces (no options venue), HFT market making, causal ML. Each would need data, infrastructure or a venue we don't have, and none addresses our measured problems.

All of the above runs on the desk server's data (the guardian and journal moved to EC2 on 2026-10-03), so the studies belong in sessions there.

## What the repos taught (study of 2026-10-04)
Six agents read the cloned repos (`learning-repos/`, Mac only). The full reports, with file paths, are in `learning-repos/_study-2026-10-04/`. This section keeps what changes our plans. The 12 strategy cards it produced, and evidence for 15 existing cards, are in docs/strategies (see "Repo study 2026-10-04").

### The cautionary tale: ML4T's own Binance-perps case study
Jansen's `case_studies/crypto_perps_funding` ranked 19 perps every 8h on premium, funding and volatility. Of 2,807 candidates, the chosen setup had a validation Sharpe of 1.67 (PSR p 0.005, bootstrap CI excluding 0). Corrected for the 2,807 trials, its Deflated Sharpe was below 0. On the 2024-25 holdout: Sharpe −0.83, CAGR −45%, max drawdown −82%, with execution costs above 4× the funding earned. The trial register exists to catch exactly this.

### Process and architecture to adopt
1. **A forecast layer (Carver).**
   - Today `signals/` add points into a 0-100 score. Signals on different scales dominate, correlated ones double-count, and nothing maps score to size.
   - Instead, each plug-in also emits a signed raw forecast. Scale it to an average |F| of 10 and cap at ±20.
   - Combine the forecasts with cluster weights (pump/froth, funding/OI, flow, lifecycle) times a forecast diversification multiplier.
   - Size each plan's R by clip(F_c/10, 0, 2), and skip trades with small |F_c|.
   - Adopt only if F_c deciles are monotone in realised R.
2. **An LLM research loop with a deterministic gate** (RD-Agent's loop, not its acceptance rule).
   - RD-Agent runs propose → implement → run → feedback → archive, keeping (experiment, feedback) pairs and an accepted-factor library.
   - It accepts on an LLM's judgement of one fixed test window ("any small improvement should be considered"), with no significance or multiple-testing control.
   - Ours: a hypotheses table in `trials.db`, a trace brief at session start, a novelty check on score correlation (~0.7), judgement by increment over the accepted set, and acceptance only through the register's statistics (DSR at effective N, clustered t, placebo rank).
3. **A factor expression engine** (Qlib `data/ops.py`, MIT).
   - Factors written as strings hash straight into the trial spec, so duplicates are caught exactly, and the agent proposes text rather than code.
   - Each operator declares how far forward it reads, so look-ahead is rejected at parse time.
4. **Count every sweep and deduplicate** (ML4T).
   - Overlay, sizing and exit variants are trials too. Sizing alone moved crypto Sharpe by 0.908 across six allocators.
   - Drop trials with identical return series before deflating (`uncertainty.py::_distinct_trials`).
5. **A bias-check harness** (the idea of freqtrade's lookahead and recursive analysis, reimplemented since freqtrade is GPL-3).
   - Recompute every feature on data truncated at each event and compare.
   - Vary the warm-up length and compare the last row.
   - Run it in CI over `signals/` and `mtf.py`.
6. **Backtest realism.**
   - Stop first when stop and target hit in the same bar (hummingbot does the opposite).
   - Impact cost growing with (order size / bar volume)², plus volume caps and can't-trade masks (Qlib `backtest/exchange.py`).
   - A fill model calibrated from our own fills.
   - Purge horizon + 1 bars before each test window in rolling refits.
7. **Per-coin protections** (freqtrade). The governor is account-level. Add a per-coin cooldown after a stop-out and a per-coin stop-loss guard (N stop-outs in a window lock the coin); a low-profit lock is optional.
8. **Production habits.**
   - Half-open circuit breakers: after a halt, let one observation through before resuming (ML4T ch. 26).
   - Go-live thresholds declared in advance.
   - Funding P&L reported apart from price P&L.
   - Carver's 4-measure portfolio risk overlay above the governor. Each measure has a limit: EW-covariance σ ≤ 1.75τ; σ with every vol at its 99th percentile ≤ 4τ; Σ|w|σ ≤ 4τ; gross leverage ≤ 20. The shocked-vol measure prices the squeeze regime.

### Formulas to reimplement (Carver; never copied, pysystemtrade is GPL)
- **Forecast scalar:** k = 10 / mean_t(median_i |F_raw|), pooled and expanding, at least 500 observations; F = clip(k·F_raw, ±20).
- **Combination:** F_c = clip(FDM · Σ w_j F_j, ±20). FDM = min(1/√(wᵀHw), 2.5), where H is the correlation of the forecasts (weekly, negatives floored at 0), smoothed by an EWMA of span 125.
- **Vol target:** N = (capital·τ/√365) / (price·σ_daily) · F_c/10. Half compounding shrinks capital with losses but never grows it with gains.
- **Portfolio:** P = N · w · IDM · risk_scalar. IDM uses the same formula as the FDM, on the correlation of subsystem returns.
- **Buffer:** B = 10% of the position at F = 10. Trade only when the position leaves [P − B, P + B], and then only to the edge.
- **Speed limit:** a rule's cost in SR units = turnover × cost per trade + holding cost (for us, funding). Drop rules above 0.13 SR/yr. Carver's own Bitcoin weights zero out every fast rule for this reason.
- **Long sleeve:** F = mean(continuous 20-day breakout, RS-vs-BTC relmomentum) × alt_index_trend_ewmac, with w = 1/n and IDM from 60-day coin correlations. Stop-R from `risk_budget.py` stays a hard cap. Backtest it against fixed-R sizing on the same trades.

### Validation traps found by reading the code
- **arch SPA/StepM/MCS** (`arch/bootstrap/multiple_comparison.py`) take losses, so pass −returns. `studentize` only changes a label: `SPA.compute()` never divides by the standard deviations, so the most volatile strategy dominates. Feed unit-volatility series or write our own studentized version.
- **skfolio CombinatorialPurgedCV** purges a fixed number of rows. Our variable-horizon triple-barrier labels need purging by each label's end time, which we write ourselves.
- **hmmlearn `predict_proba`** returns smoothed probabilities that use future bars. Use statsmodels' Markov-switching filtered probabilities, or an expanding refit taking the last row.
- **Machine-Learning-for-Asset-Managers** has bugs (`getTheta` divides by .4; `DataFrame.append`). Reference only.
- **FreqAI's walk-forward** has no purge or embargo.
- **The PBO recipe** (Bailey et al. 2015, verified in the paper):
  1. Form a T × N matrix of daily P&L.
  2. Split its rows into S blocks; S = 12 gives 924 splits.
  3. For each half-split, take the in-sample best and its out-of-sample relative rank ω, and compute λ = ln(ω/(1 − ω)).
  4. PBO = P(λ ≤ 0).
  5. Also report the λ histogram, the IS-OOS degradation slope and P(OOS loss).

### Licenses: what we may copy
| repo | license | use |
|---|---|---|
| Qlib, RD-Agent, ML4T | MIT | copy with notice |
| skfolio, Riskfolio-Lib, hmmlearn, ArbitrageLab | BSD-3 | copy with notice |
| arch | NCSA | copy with notice |
| alphalens, MLfAM, hummingbot | Apache-2.0 | copy with notice |
| vectorbt | Apache-2.0 + Commons Clause | learn only (no selling) |
| nautilus_trader | LGPL-3 | learn / reimplement |
| freqtrade, pysystemtrade | GPL-3 (examples GPL-2) | learn / reimplement only |
| mlfinlab | proprietary (public repo has no code) | method list only |

**Dependency decision:**
- Add `arch` as a research-only optional extra, never in the guardian image. It brings statsmodels, whose Markov-switching model gives forward-only regime probabilities.
- Skip hmmlearn (limited maintenance), skfolio and Riskfolio.
- Reimplement in numpy: PBO, BH-FDR, minimum backtest length, uniqueness weights, HAR, Ledoit-Wolf, denoising, HRP, ERC and CDaR (each under ~40 lines).

### For the gateway (the gateway owner's area)
From pysystemtrade's production layer and NautilusTrader's risk engine, ordered by value:
1. A per-sleeve position table (short book, long sleeve) with an internal and an external position-break check. An external break locks the symbol until fixed, and the lock clears itself.
2. A reference price and time on every intent. That makes TCA nearly free, split into delay, bid-ask and execution.
3. Trade limits per N days, as a runaway-loop guard.
4. Override algebra: no-trade > close > reduce-only > a product of scale factors.
5. Typed pre-trade checks:
   - a reduce-only order must not flip the position;
   - margin is checked summed across tranches;
   - order-count throttle.
6. Handle triggered conditional orders explicitly.
7. Treat a missing position report as unknown, not flat.

One claim to verify before acting: the engines report says the user-data listenKey needs a keepalive every 30 min, while GATEWAY.md says 60. Check Binance's current futures docs.

### Updated adoption order
1. **PBO + SPA/StepM + BH-FDR + minimum backtest length**, plus deduplication of identical trials, on the register (#43).
2. **Re-pump hazard and market regime** (#44):
   - transitions of `tape_math.pump_state` recomputed hourly per coin (no look-ahead);
   - statsmodels Markov-switching for the market regime;
   - `alt_index_trend_ewmac` as the continuous gate.
3. **Squeeze vetoes on pump_fade:** VPIN / Kyle's λ and SADF timing, each against a random veto at the same rate (cards vpin_toxicity_squeeze_veto, sadf_lppls_bubble_short).
4. **The forecast layer and Carver sizing:** combination for the short book; vol targeting for the long sleeve.
5. **Meta-labeling** with label-end-time purging and uniqueness weights (#45).
6. **Bias-check harness and backtest realism** (same-bar stop first, impact, volume caps).
7. **Per-coin cooldown and stop-loss guard.**
8. **The research loop's deterministic gate and the expression engine.**

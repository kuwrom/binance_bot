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

## What to adopt, in order
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

# Failure analysis and the improvement loop

> **Statistics note (#31, 2026-10-02):** the clustered t-statistics and Deflated Sharpe ratios quoted here come from the **pre-#31 estimator**: a t over equal-weighted day (or coin / hour) means, not the t of the pooled mean reported beside it, and a DSR with T = the trade count and N = one run's trials. They are kept as recorded. A rerun reports the t of the pooled mean (day, coin and two-way) and a DSR on the effective sample with N from the trial register: docs/BACKTEST.md "Clustering and multiple testing".

Each round has four steps. **Diagnose** only on in-sample trades, meaning entries before the window midpoint (2026-08-29 19:30 UTC). **Pre-register** at most 5 fixes, with thresholds written down here *before* any out-of-sample (OOS) run. **Run** every variant once. **Judge** each one with the same bar as `docs/BACKTEST.md`. Every variant is added to the trial ledger, so the Deflated Sharpe counts every idea ever tried.

Code:
- `backtest/failures.py`: the loss classifier (removed 2026-10-03 as unused; in git at f142b6f).
- `backtest/strategies_v2.py`: the variants and `ledger()`.
- Run: `.venv/bin/python -m shortbot.backtest.run --days60 --round1 --out DIR`.
- Tests: `tests/test_backtest_v2.py`.
- Round 2: `backtest/strategies_v3.py` (variants), `backtest/report_v3.py` (tables, continuation stats), `--round2`, `tests/test_backtest_v3.py`.


## Rounds
| round | question | file | headline |
|---|---|---|---|
| 1 | Where did the 60-day strategies fail? | [failures/ROUND1.md](failures/ROUND1.md) | Failure modes mapped. pump_fade+discount became a shadow candidate. The tide veto cuts losses. Weekend, taker and stop-floor filters overfit. |
| 2 | Which entry type works in breakdowns, and what are BE/trailing worth? | [failures/ROUND2.md](failures/ROUND2.md) | Resting bounce limits get adversely selected. Trailing adds nothing to fades (off). Breakeven is roughly neutral (kept). Re-entry rejected. |
| 3 | Are the live vetoes right? | [failures/ROUND3.md](failures/ROUND3.md) | Negative-funding veto blocks the best sub-book (drop or soften). Keep the premium flag, the perp-led bonus and the flush veto. The taker and spot-led vetoes do about nothing. With 123 trials, pump_fade+discount's DSR is 0.01. |
| 4 | Were the controls right? (matched placebo, effective-N DSR, 5m exits) | [failures/ROUND4.md](failures/ROUND4.md) | The relative edge of pump_fade / post_pump_decay / bounce_fade was mostly a cost mismatch: matched, it is +0.02 / +0.00 / +0.14 (t ≤ 1.4). pump_fade+discount holds (+0.14 net, +0.16 gross, t 3.1), with DSR 0.38 at N_eff 48 (0.01 before). On 5m bars trailing still adds nothing. |

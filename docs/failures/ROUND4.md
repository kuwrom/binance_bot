# Round 4 (2026-09-29): corrections to rounds 0–3

A read-only code review found five problems in the backtest. None of them is a new idea or a new trial. They are fixes to the **controls**: the placebo and the multiple-testing haircut. The ledger is unchanged (N = 123 trials), and no strategy rule changed. Rounds 0–3 were then rerun once with the fixes.

Code:
- `backtest/placebo.py`: the matched placebo.
- `backtest/trials_eff.py`: effective N and the family V[SR].
- `backtest/report_v5.py`: the corrections tables and the verdict diff.
- `backtest/hist5m.py` and `backtest/trail5m.py`: the 5-minute exit replay.
- Tests: `tests/test_backtest_fixes.py`.

The old behaviour is kept behind flags. `--placebo=legacy --dsr=legacy` reproduces rounds 0–3 exactly.

    .venv/bin/python -m shortbot.backtest.run --days60 --round2 --round3 --round4 --compare OLD/results.json --out DIR
    .venv/bin/python -m shortbot.backtest.trail5m --fetch --out DIR

Run outputs: `bt60_fixed/` in a session scratchpad (report.md, results.json, trades.json, trail5m/). **Lost**: rerun it and cite the stamped `data/research/` dir (docs/BACKTEST.md "Provenance"); once hist.db has grown, pin the 09-29 build's range, `--t0 2026-07-27 --t1 2026-09-29T15:00` (unchanged as of 10-01).

### 1. What was wrong, and the fix

| # | problem | fix |
|---|---|---|
| 1 | **The same-hour placebo was not volatility-matched.** Signal coins had 2.9–5.8% ATR, while the random placebo coin had about 1.8%. The fees and slippage are the same in %, so they cost the donor fewer R: 0.02–0.05R per trade against 0.09–0.19R for the placebo. The *net* paired edge was mostly this cost gap. | Draw the placebo coins from the donor's cross-sectional **ATR% decile × 24h-volume bucket** ($1–2M, 2–5M, 5–20M, 20–100M, ≥ 100M) at the same hour. If that cell has too few coins, take the nearest coins by ATR and volume percentile; 97–100% of draws landed in the cell. Report the paired edge **net and gross** (`r_gross`). Matched placebos are simulated one by one, not under the runner's one-trade-per-coin rule, because they are controls and not a book. |
| 2 | **The DSR counted clones and tiny partitions.** Clones (e.g. `+exit_be` re-runs its base, and a no-op funding budget equals its base) counted as independent trials. Partitions with n 10–60 inflated V[SR] with sampling noise. | Cluster the trials by the correlation of their daily R: average linkage, merging clusters that correlate > 0.7. **N_eff = 48** clusters (16 of them multi-member), against a raw N of 123. **V[SR]** is now taken across families using members with n ≥ 100: **0.0104**, against 0.0253 before. The DSR is reported under raw N and under N_eff. |
| 3 | BACKTEST.md's dataset line was stale: 649 perps / 130 delisted, from the first build with its zombie perps. | Updated: **528 perps** (520 trading, 8 settling), all 8 of them delisted **inside** the window. |
| 4 | "Trailing adds nothing" was judged on 1h bars, which cannot order the events inside a bar. | Replayed the same signals on **5-minute bars** (item 4 below). |
| 5 | **Placebo parity.** The fund-budget trials' placebos had no budget exit. Round 0's `level_fade` limit placebos entered at the *signal* bar, not the fill bar. | In matched mode the placebo carries the donor's funding budget and enters at the **fill** bar for every non-market entry. The budget exit fired on 0 placebos (and 0 donors), so that part is a no-op. |

### 2. Corrected relative edge

Paired edge vs the same-hour placebo, in R per trade. The t-stat is day-clustered; IS and OOS are the two halves of the window. The last column is ATR%: donor / legacy placebo / matched placebo.

| trial | n | legacy net | legacy gross | **matched net** | **matched gross** | ATR% d / L / M |
|---|---|---|---|---|---|---|
| pump_fade (1 ATR, 12h) | 2211 | +0.09 (t 3.7) +0.10/+0.08 | +0.04 (1.1) | **+0.02 (t 0.9) +0.05/−0.01** | **+0.02 (1.1) +0.05/−0.00** | 2.9 / 1.8 / 3.0 |
| post_pump_decay (2 ATR, 12h) | 2856 | +0.07 (t 3.8) +0.09/+0.04 | +0.02 (1.0) | **+0.00 (t 0.2) +0.02/−0.02** | **+0.00 (0.2)** | 3.2 / 1.9 / 3.1 |
| bounce_fade (−15%, 24h) | 198 | +0.23 (t 2.5) +0.21/+0.27 | +0.10 (1.1) | **+0.14 (t 1.4) +0.07/+0.27** | **+0.14 (1.5) +0.05/+0.29** | 5.8 / 1.8 / 5.7 |
| pump_fade+discount | 133 | +0.22 (t 4.0) +0.18/+0.26 | +0.18 (3.4) | **+0.14 (t 3.1) +0.12/+0.15** | **+0.16 (3.5) +0.15/+0.18** | 4.5 / 1.7 / 4.8 |
| level_fade (round, market, 24h) | 9153 | +0.02 (t 1.7) +0.04/−0.01 | −0.01 (−0.3) | **+0.00 (t 0.5) +0.03/−0.02** | **−0.00 (0.2)** | 2.1 / 1.9 / 2.1 |
| level_fade (round, limit, 24h), parity fix | 5368 | +0.28 (t 7.7) +0.27/+0.29 | +0.18 (3.9) | **+0.06 (t 1.7) +0.07/+0.05** | **+0.03 (0.7)** | 2.3 / 1.8 / 2.4 |

Once the placebo is matched, net ≈ gross in every row. That confirms the mechanism: the old net-minus-gross gap was pure cost mismatch.

The two books with the budget exit show the size of seed noise. pump_fade+discount+fundbudget has the same donor trades as pump_fade+discount, but its placebo is drawn with a different seed, and its edge is +0.09 (t 3.0) instead of +0.14. So about ±0.05R of any edge above is placebo sampling noise.

### 3. Deflated Sharpe, pump_fade+discount

Per-trade SR is 0.202 on n 133, and PSR(0) is 0.99.

| | N | V[SR] | SR\* | DSR |
|---|---|---|---|---|
| legacy (round 3) | 123 | 0.0253 | 0.41 | 0.01 |
| raw N, family V[SR] | 123 | 0.0104 | 0.265 | 0.25 |
| **effective N, family V[SR]** | **48** | 0.0104 | 0.230 | **0.38** |

It is still far from 0.95. The book is not significant after the haircut, but it is no longer the "0.01" of round 3.

### 4. Trailing on 5-minute bars

The 5-minute bars come from data.binance.vision daily 5m klines: 3,666 coin-days downloaded (13 not yet published), cached in `data/hist5m.db` with concurrency 8 and a pause after each request. Coverage was 2200 of 2211 pump_fade signals and 193 of 196 bounce_fade signals.

The table shows Δ against fixed exits on the same signals and at the same resolution: the mean, then the day-clustered t, then IS/OOS.

| family | exits | Δ at 1h | Δ at 5m | round-2 rule, 1h → 5m |
|---|---|---|---|---|
| pump_fade | + live trail | +0.000 (t 0.6) +0.002/−0.001 | −0.000 (t −0.1) +0.001/−0.001 | neutral → neutral |
| pump_fade | + breakeven 1R | +0.007 (t 3.4) | +0.007 (t 3.5) | neutral → neutral |
| pump_fade | both | +0.007 | +0.006 | neutral → neutral |
| bounce_fade | + live trail | −0.045 (t −2.0) −0.01/−0.10 | −0.037 (t −1.9) +0.01/−0.12 | off → off |
| bounce_fade | both | −0.023 | −0.015, OOS −0.10 | off → off |
| bounce_fade | + breakeven 1R | +0.019 +0.02/+0.01 | +0.019 +0.03/+0.01 | on → on (t −0.6) |
| bounce_fade | current (be + 2R/1R trail) | +0.034 | +0.036 | on → on (t −0.6) |

**Verdict:** round 2 stands at 5-minute resolution. The live trail is neutral on the pump family and hurts bounce_fade OOS. The average R of every variant moves by ≤ 0.008R between 1h and 5m, so the 1h bar-ordering assumption was not driving the result.

### 5. Which conclusions change (24 verdict changes vs the round-3 run, `report.md` "Verdict changes")

1. **"Relative edge only" is withdrawn.** All 23 strategy-level changes are `fails (relative edge only)` → `fails`: pump_fade, post_pump_decay and 21 of their variants (exit, tide, stack, veto-allowed, re-entry). bounce_fade's base book was already a plain "fails" in round 3, because its t of 2.5 is below the bar of 3.
   - "The trigger beats shorting another coin that hour" was mostly the cost mismatch.
   - The hedged pump-family idea (rounds 0 and 1) loses its premise.
   - bounce_fade's +0.14 is not significant (t 1.4, IS +0.07).
2. **level_fade limit fills:** the "+0.28 vs placebo" came from the signal-bar placebo. At the fill bar it is +0.06. The round-2 **adverse-selection** finding is unchanged: it compares entry types on the same arms and uses no placebo.
3. **Breakdown entry types (round 2 Q1):** the relative edges shrink from +0.08…+0.28 to −0.01…+0.09 (t ≤ 2.0). All of them were already rejected, and the sell-stop-over-limit rule is unchanged.
4. **Round-3 vetoes:** one verdict flips. spot_led on level_fade goes from KEEP (marginal) to **DROP**, which matches round 3's overall call of "no stable effect". Every other veto verdict is unchanged, including the negative-funding **DROP** on pump_fade and level_fade: those verdicts rest on the absolute OOS R, and the blocked edge still ties or beats the allowed one.
5. **pump_fade+discount is unchanged and slightly stronger.** It is the only book whose edge survives matching: +0.14 net / +0.16 gross (t 3.1 / 3.5), positive in both halves. Its verdict stays **adopt-to-shadow**, with DSR 0.38 at N_eff 48. Its 12h fixed-exit book still needs the forward shadow window.
6. **Unchanged:** every absolute-R number, the stress runs, the fact that nothing SURVIVES, and every trailing / breakeven call.

The mid-window data also answers "was the trend down?". Of the 516 coins that span the whole window, only **20% ended lower**. The median coin rose +25.5%, and BTC +27.5%. This window was a rising market for alts, which is why every absolute short book is negative.

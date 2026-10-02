# Multi-year regime studies (research 2026-10-02)

This file has three daily studies plus the remaining card tests (§4) on six years of Binance perps, delisted coins included. The hourly backtest in [BACKTEST.md](BACKTEST.md) covers only 60 days, which is one regime. Each study was fixed in code before its first run, and every cell is logged as a registry trial.

**Data:** `histdaily.py` → `data/daily.db`:
- 1d klines and funding for **676 USDT coin perps**;
- 2020-01 to 2026-08;
- 616k bars and 2.65M funding prints;
- from the data.binance.vision monthly archives.

To rebuild: `.venv/bin/python -m shortbot.histdaily build`.

**Conventions:**
- Entry is at a day's close.
- Returns are for the short side and include the funding the short receives.
- A coin that stops trading inside a window exits at its last close (delivery). A window that runs past the end of the data has no exit.

## 1. Does high carry mark a crash regime? (task #12, `backtest/carry_regime.py`)
**Hypothesis** (library card `carry_crash_regime`, BIS WP 1087): high BTC/ETH carry marks crowded, fragile longs, so shorting alts should pay more and crashes should follow.

**Carry:** the mean over BTC and ETH of the 7-day mean daily funding, × 365. Its percentile is taken against the previous 365 days only, with at least 180 days of history. Over the window, carry has p10 0.1%, median 6.8% and p90 32% annualised, across 2,459 days.

**Regimes, horizons and outcomes:**
- 5 regimes:
  - top decile;
  - top quintile;
  - bottom quintile;
  - carry ≥ 20%;
  - carry ≤ 0.
- 7 outcomes:
  - alt-basket short over 7d and 30d (equal weight, listed ≥ 30d, ≥ $5M volume);
  - BTC short over 7d and 30d;
  - fading coins up ≥ +20% on the day, held 1d and 3d;
  - an alt crash: the basket's worst close within 30d is ≤ −25%.
- **Statistic:** regime minus the rest, with a Newey-West t (lag = horizon) and a per-year split.

**Results:** 35 cells. Positive values would mean shorts do better in the regime.

| regime → outcome | regime days | in regime | rest | diff | t (NW) | years same sign |
|---|---|---|---|---|---|---|
| top quintile → alt short 7d | 413 | −4.32% | +0.11% | **−4.44%** | **−3.10** | 4/5 |
| top decile → alt short 7d | 211 | −4.92% | −0.26% | −4.66% | −2.57 | 3/3 |
| carry ≥ 20% → alt short 7d | 323 | −4.92% | +0.01% | −4.93% | −2.56 | 3/3 |
| top quintile → alt short 30d | 407 | −14.7% | −1.0% | −13.7% | −1.82 | 2/4 |
| top quintile → alt crash 30d | 407 | 12.5% | 24.0% | −11.5 pp | −1.93 | 2/4 |
| carry ≤ 0 → alt crash 30d | 228 | 8.3% | 23.5% | −15.1 pp | −2.49 | 2/2 |
| any regime → top-gainer fade 1d/3d | – | – | – | −1.5% to +2.1% | −0.8 to +1.3 | – |
| any regime → BTC short 7d/30d | – | – | – | −3.0% to +2.1% | −1.3 to +0.7 | – |

The top-quintile 7-day effect by year was 2020 −5.4%, 2021 −6.3%, 2023 −2.6%, 2024 −4.8% and 2026 YTD **+3.3%**.

**Conclusions:**
- **The BIS claim does not hold on our venue at 7-30 days.** High aggregate carry is trend-on. The alt basket keeps rallying (shorts −4.4% over 7 days), and crashes are *less* frequent afterwards, not more.
- After 35 trials, t −3.1 sits close to a Bonferroni bar (~3.2). It is consistent across 4 of 5 years, but 2026 is reversed. Treat it as **supported**, not proven.
- **What it is:** market beta, not alpha. BTC shows no significant effect. Fades of single top-gainers (our setup family) show **no** regime effect.
- Negative carry marks the period *after* a crash: crashes are less frequent from there.
- **Rule** (library card `high_funding_trend_confirm`): when aggregate carry is in its top quintile, cut the size of broad or beta alt shorts. Setup-level fades need no gate.
- **Registry:** `lib.carry_crash_regime` is retired.

## 2. Does a coin's own high funding predict a fall? (task #13, `backtest/funding_events.py`)
**Hypothesis** (cards `high_funding_squeeze_veto` and `high_funding_trend_confirm`): high funding is a short signal on post-pump alts but a trend-on signal on majors, so "never fade majors on funding".

**Event:** a coin's average funding per 8h over a day is ≥ 0.05% or ≥ 0.10%. Only the first day of a run counts, followed by 7 quiet days.

**Classes** (decided per day, with no look-ahead):
- **major:** top 10 by 30-day volume;
- **pumped alt:** a non-major up ≥ 30% over 7 days;
- **other alt:** the remaining non-majors.

**Entries:**
- **raw:** short at the close;
- **breakdown:** short at the first close below the prior day's low within 3 days.

**Measurement:** 1, 3 and 7-day short returns net of 0.10% fees. The **edge** is the return minus the class mean over the **same window**, which cancels the market move. The statistic is the mean of day-means with a day-clustered t.

**Results:** 21,251 trades over 36 cells.

| cell (thr 0.05%) | n | coins | mean ret | edge | t (edge) |
|---|---|---|---|---|---|
| raw · major · 1d / 3d / 7d | 203 | 58 | −1.6% / −2.1% / −2.0% | −0.5% / −0.8% / +0.6% | −0.6 / −0.5 / +0.3 |
| raw · pumped alt · 1d / 3d / 7d | 583 | 299 | −0.3% / −0.3% / −3.6% | −0.2% / +0.5% / −0.9% | −0.2 / +0.4 / −0.4 |
| raw · other alt · 7d | 2,551 | 477 | −4.3% | −4.0% | −1.8 |
| breakdown · major · 1d | 97 | 46 | +3.0% | +2.3% | 2.24 (the only "works" cell) |
| breakdown · other alt · 7d | 1,218 | 397 | −3.0% | −2.4% | −2.0 |

The 0.10% threshold gives the same picture: every |t| < 2.

**Conclusions:**
- **A coin's own high funding carries no short edge** against same-class peers over the same window, in any class, at 1-7 days. No cell shows "avoid". The single "works" cell (t 2.24) is what chance gives among 36 trials.
- **Absolute short returns are negative on high-funding days**, especially for majors and over 7 days. That comes from the market regime (study 1), not from the coin.
- **Practical upshot:**
  - **Never short on funding alone**, in any class. This now applies beyond majors: PLAYBOOK says "never a trigger on its own", and round 0 found funding_hi failed in 60 days.
  - Keep coin funding as a flag: the funding budget and a tailwind.
  - Do the sizing with the aggregate-carry gate from study 1.

**Two code fixes were made before these results were read, and are covered by tests:**
- The breakdown control had started on the event day instead of the entry day.
- The verdict had taken its sign from the pooled mean but its t from day means. Heavy tails (coins 5x-ing) made those disagree.

## 3. Cross-sectional factor books (task #19, `backtest/xs_factors.py`, `xs_features.py`)
**Hypothesis** (cards `ctrend_technical_aggregate` and `carry_momo_breakout_blend`): technical rankings and the Robot Wealth blend earn a cross-sectional premium on Binance perps, mostly in the long leg.

**CTREND:**
- Weekly, top-100 perps by volume.
- 21 daily indicators, rank-normalised, combined by ridge regression refit every week on the previous 52 completed weeks. This replaces the paper's elastic net, and every prediction is out of sample.
- Quintile long-short, 15 bp per unit of turnover, funding included.
- 291 weeks, 2021-03 to 2026-08.

**Blend:**
- Daily, top-30 perps.
- 0.5 carry (−funding) + 0.2 10-day momentum + 0.3 breakout, as cross-sectional z-scores.
- Dollar-neutral, gross 1, with a 0.02 no-trade buffer.
- 2,355 days, 2020-03 to 2026-08.

| book | leg | mean | t | Sharpe (ann.) | by year |
|---|---|---|---|---|---|
| CTREND | long-short gross / net (per week) | −0.07% / −0.27% | −0.36 / −1.44 | −0.15 / −0.61 | negative 2021, 2023, 2024; positive 2022, 2026 |
| CTREND | long leg, net | −0.31%/wk | −0.47 | −0.20 | tracks the universe (+0.19%/wk, t 0.3) |
| CTREND | short leg, net | −0.22%/wk | −0.30 | −0.13 | the mirror |
| Blend | long-short gross / net (per day) | +0.23% / +0.16% | 2.17 / **1.55** | 0.86 / 0.61 | net: 2020 +90%, 2021 +116%, **2022 −66%**, 2023 +31%, **2024 −20%**, 2025 +171%, 2026 +123% (annualised means) |
| Blend | long leg / short leg (gross) | +0.24% / −0.01% per day | 2.14 / −0.23 | 0.84 / −0.09 | the long names carry the whole book |

**Conclusions:**
- **CTREND does not replicate** on liquid Binance perps after 2021. The paper's 2015-2022 result (>3,000 spot coins, much of it small caps) does not carry over to the top-100 universe we can trade, at least not with a ridge combination. Both legs are noise around the universe.
- **The blend is the best systematic book found so far, but it is not significant net of costs.**
  - Net Sharpe is 0.61 (t 1.55), with two losing years.
  - The edge sits entirely in the long leg (high momentum, near 20-day highs, low funding). The short leg earns nothing.
  - This repeats the academic finding: factor premia live in the long leg, so a short book cannot harvest them.
- Note: trials are logged under source `xs_daily`, 6 legs in total.

## 4. The remaining testable cards (task #28, `backtest/card_tests.py`)
Each test was fixed before its run. "Excess" means against the unconditional return over the same holding length.

**Daily (BTC unless stated, 2020-2026):**

| card | rule | n | result |
|---|---|---|---|
| funding_negative_zscore_long | 30d funding z ≤ −1.5 → long until z > 0 or 30d | 89 (avg 5d) | +1.94% per trade vs +0.57% → **+1.37% excess** (excess t ≈ 1.65) |
| negative_funding_streak_long | 30d mean funding < 0 for ≥ 30 days and above the 20DMA → long | 3 (2020-04, 2021-08, 2026-04) | 30d +16.9% vs +3.4%; 60d +9.6% vs +7.4%. Anecdote (n = 3) |
| cycle_low_accumulation | > 20% under the 200DMA, 30d funding < 0, > 40% below the ATH → long | 3 (2021-07, 2022-11, 2026-02) | 90d +35.8% vs +12.6%; 180d +36.3% vs +31.3%. Anecdote |
| dominance_rotation_long | ETH/BTC above its 50DMA and BTC lagging the alts → top-5 residual-strength alts, 7d | 113 on / 221 off weeks | regime on +1.77%/wk vs off +2.10%: **the regime adds nothing**. The top-5 residual-strength alts beat the basket by +1.1-1.3%/wk in both regimes (residual momentum, long side) |

**Hourly (60 days; t clustered by event hour):**

| card | result |
|---|---|
| funding_settlement_drift | Into a print with \|funding\| ≥ 0.1%: payer side −0.19%, t −0.72, i.e. no effect. **2h after the print the payer side GAINS +0.29% (t −3.67, 1,046 prints)**: high funding keeps trending past the print, the opposite of the card. Rule: don't short a high-funding coin right after the print |
| intraday_hour_seasonality | UTC 04 / 11 / 16 / 23 are positive (hour 16 = +21bp, day-clustered t 3.9); the hourly profile correlates 0.48 between halves. About 10-20bp against ~10bp taker round trip, so a timing tweak only |
| btc_alt_lead_lag | Alts' next hour after a BTC +1% hour: +0.26% (t 1.78, only 20 BTC hours); after a BTC −1% hour: +0.14% (t 0.58). Weak, consistent with the existing veto on new shorts in a BTC up-impulse |

**Not testable with our data:** etf_flow_follow, cme_gap_fill, usdt/eth_exchange_inflow, upbit_warning_short, buyback_burn_long, liquidation_magnet, attention/adoption. Each needs an external feed we don't record.

## Rerun
```
.venv/bin/python -m shortbot.backtest.carry_regime --record     # JSON + provenance.json in data/research/<date>-<sha7>/carry_regime/
.venv/bin/python -m shortbot.backtest.funding_events --record
.venv/bin/python -m shortbot.backtest.xs_factors --record
.venv/bin/python -m shortbot.backtest.card_tests            # and --hourly
```

# Long playbook (research 2026-10-02, task #32)
Which long strategies the desk follows, and how. Every rule here comes from a pre-registered test on our own data. Evidence: docs/REGIME_STUDIES.md §5 (daily, 2020-2026) and docs/BACKTEST.md "Long sleeve" (hourly, 60 days).

## The short answer
- **Two long setups have evidence:** relative-strength leaders (weekly) and 20-day breakouts (5 days). Both work at daily and weekly horizons only. Every hourly long trigger tested so far fails against its placebo.
- **Both edges live in the tail.**
  - The median trade *underperforms* the market by about 2.2%.
  - The top 5% of trades supply 235-270% of the total excess.
  - So: take every signal, size them equally, and never cap the winners with targets.
  - A desk that picks one or two "best-looking" names, or takes profit at 2-3R, gets the median outcome, which is a loss.
- **A trend gate decides whether longs are allowed:** BTC's 60-day return > 0. It cuts drawdowns by a fifth to a third at the same mean return.
- **Status: not deflation-proof** against all 270 effective trials the desk has run (t 2.2-2.4 vs a ~3.3 noise maximum). It does pass against the long family alone (bootstrap p 0.001-0.008). So both setups start in **demo at C size (0.5x), traded mechanically, as a forward test** with acceptance rules fixed now (below).

## Gate G: when longs are allowed (card tsmom_trend_following)
**Rule:** new longs only while BTC's close is above its close 60 days earlier. When G turns off, open longs run to their own exits, and no new ones are opened.

| 2020-2026, daily | mean/day | Sharpe | max drawdown | total |
|---|---|---|---|---|
| BTC, buy and hold | +0.115% | 0.69 | −79% | +347% |
| BTC, gated (55% in the market) | +0.108% | 0.93 | −61% | +621% |
| Top-30 alt basket, buy and hold | +0.106% | 0.44 | −92% | −9% |
| Top-30 alt basket, gated by BTC | +0.105% | 0.63 | −74% | +259% |

- The gate mostly cuts risk.
  - Next-day BTC return is +0.20% on gate-on days vs +0.01% on gate-off days (t 1.4).
  - Volatility is lower on gate-on days (3.0% vs 3.4%).
- The 20-, 120- and 200-day versions behave the same way; r60 was the pre-registered primary.
- Holding alts unhedged is terrible even with the gate (−74% drawdown). That is why the setups below are single trades with exits, not buy-and-hold.

## L1 · Relative-strength leaders (cards beta_hedged_rs_pair, dominance_rotation finding)
- **Trigger:** every Sunday at 00:00 UTC, take the top 30 perps by 30-day volume (excluding BTC). Rank them by their 28-day return *residual to BTC* (beta from 60 daily returns), and long the top 5.
- **Entry:** at that close. An intraday pullback entry was tested and adds nothing (rs_leader_pullback, below).
- **Exit:** the next Sunday's re-rank.
  - Names still in the top 5 roll over without trading.
  - Names that drop out are closed.
  - No profit targets and no breakeven stop.
  - Only a catastrophic stop (3× daily ATR(14) under entry) protects against a delisting or hack.
- **Size:** equal across the 5 names, each 1/5 of a normal single-trade risk budget. C (0.5x) during the forward test.
- **Gate:** only while G is on. The excess vs the basket was also positive in gate-off weeks (+2.2%, t 2.1), but unhedged longs then ride a falling basket. A BTC-beta-hedged variant for gate-off weeks is untested (follow-up).

| evidence (lb28; lb7 / lb14 nearly identical) | value |
|---|---|
| trades (name-weeks) | 1,658 over 249 coins |
| mean excess vs the top-30 basket, per name-week, after 10 bp and funding | **+1.76%** (two-way t 2.40) |
| 4-week block bootstrap | 95% CI [+0.57%, +3.20%], P(≤ 0) 0.25% |
| by year | 2020 +2.1%, 2021 +2.0%, 2022 −0.1%, 2023 +0.4%, 2024 +1.3%, 2025 +3.5%, 2026 +4.0% |
| median / 5%-trimmed mean | −2.4% / −0.6%: most picks lag; a few leaders go parabolic |
| first vs second half | 2020-22 +1.3% · 2023-26 +2.1% |

## L2 · 20-day breakout, 5-day hold (card breakout_20d_overlay)
- **Trigger:** a top-50 perp's daily close exceeds the highest high of the previous 20 days, its first such close in 20 days. Long at that daily close (00:00 UTC).
- **Exit:** the 5th daily close.
  - Holding longer decays the edge: 10d +0.8% (t 1.1), 20d +0.9% (t 0.9).
  - A 3×ATR chandelier trail does worse (+0.7%, t 0.7).
  - No targets. A catastrophic stop under the 20-day low or 3× ATR, whichever is wider.
- **Size:** equal per signal, C (0.5x) during the forward test.
- **Gate:** only while G is on. Gate-on: +1.81% (t 2.0). Gate-off: +0.94% (t 0.9).
- **Never on hourly breakouts:** buying a 3- or 7-day high on a 3× volume bar did *worse* than buying a random coin in the same hour. Paired vs placebo: −0.04 to −0.09R, t down to −2.1.

| evidence | value |
|---|---|
| trades | 2,007 over 292 coins |
| mean excess over 5 days, after 10 bp and funding | **+1.54%** (two-way t 2.18) |
| 4-week block bootstrap | 95% CI [+0.28%, +2.99%], P(≤ 0) 0.75% |
| by year | 2020 +0.7%, 2021 +2.0%, 2022 −0.3%, 2023 +0.8%, 2024 +0.0%, 2025 +3.4%, 2026 +5.1% |
| median / 5%-trimmed mean | −2.2% / −1.2% |

## Shadow only (logged, not traded)
| setup | evidence | why not traded |
|---|---|---|
| BTC negative-funding z ≤ −1.5 (funding_negative_zscore_long) | 89 trades, +1.37% excess, t ≈ 1.65 | below the bar; BTC only (the alt version is null: +0.15%, t 0.6) |
| squeeze_fuel_long (hourly) | 49 trades, +0.34R, +0.13R vs placebo (t 0.3) | too few trades |
| premium_discount z ≤ −3 (hourly) | +0.03R vs placebo (t 2.6) | the edge is smaller than costs under stress |

## Rejected (do not trade)
- **Catching coin-specific crashes** (capitulation_forced_selling_long): a −30% week that is ≥ 20 pp worse than the market loses 0.9-1.9% vs the market. In gate-on markets it loses 2.8-6.4% (t −2.1 to −2.5): crashed coins keep falling.
- Oversold bounces, liquidation-flush reclaims, the 7-day capitulation trigger (hourly): no edge beyond direction (BACKTEST.md "Long sleeve").
- Hourly breakout continuation and RS-leader pullbacks: worse than or equal to placebo.
- Alt negative-funding longs, CTREND, the dominance regime: null.

## Execution rules that follow from the tail
1. **Take every signal.** Don't let the agent veto L1/L2 picks on chart reads: the median pick loses, and the edge is in the ones that look stretched. Discretion applies only to hard vetoes (delisting, hack, exchange problems).
2. **No profit targets, no breakeven-at-1R, no trailing.** Exits are the time exit and the catastrophic stop, as tested. The short book's defaults (targets, BE at 1R) must not be applied to longs.
3. **Many small, equal positions.** About 5 L1 names plus 0-10 L2 signals a week. Size by daily ATR so each name risks the same.
4. **Costs and funding are included.** Longs pay positive funding; the results already net it out.

## Forward test (fixed now, before the first long)
- **Shadow from day one** (`long_shadow.py`, docs/LONG_BUILD.md phase 0). Every L1/L2 signal is a shadow trade at the daily close, the backtest's own fill assumption. The test starts 2026-09-01, the first day after the backtest's data; the rules were fixed without seeing any of it.
- **Demo trades at C size** follow, mechanically, once the long order path exists (phase 2). Shadow and demo results are reported separately. Demo measures what shadow can't: fills, slippage and the guardian's exits.
- Duration: 12 weeks or until 60 L1 name-weeks and 60 L2 trades (gate-on), whichever is later.
- **Pass:** forward mean excess > 0, and above the lower end of the backtest's 95% interval scaled to the forward sample (one-sided 5%).
- **Fail:** two consecutive 4-week blocks below −2× the backtest's per-block standard deviation pause the setup (the setup ladder's halving logic). After that, a retest on new data.
- Live sizing above C needs the pass plus ≥ 30 trades with a proven edge (the governor's 1.5x rule, applied to longs).
- **Read the forward numbers through the tail.** With a median trade near −2%, a few weeks will look bad and a few great. The first shadow month (September) had L1 at +10.4% mean / −12.8% median over 15 name-weeks and L2 at −1.7% over 39 trades. That is noise at this n; only the acceptance rules decide.

## Implications for the short book (hypotheses, not yet tested as shorts)
- Hourly volume breakouts underperform the same-hour placebo, which is consistent with fading hourly pumps (pump_fade).
- Coin-specific crashes in gate-on markets keep underperforming. That is a continuation-short candidate; it needs its own pre-registered test.

## Rerun
```
.venv/bin/python -m shortbot.backtest.long_studies            # daily battery, data/research/<date>-<sha7>/long_studies/
.venv/bin/python -m shortbot.backtest.run --days60 --long2    # hourly round 2 (matched placebos)
```

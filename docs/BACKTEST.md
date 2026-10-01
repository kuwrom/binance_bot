# Strategy backtest (research 2026-09-29)

`src/shortbot/backtest/` replays every strategy the docs describe on cached hourly history, with costs, placebos,
walk-forward and a multiple-testing haircut. Read-only: it never touches the exchange or the journal.

    .venv/bin/python -m shortbot.backtest.run --days60 --out /tmp/bt60     # 60d (data/hist.db); drop --days60 for the 10d cache

| file | what |
|---|---|
| `sim.py` | event-driven hourly simulator for one trade with a plan: a short, or a long with `Plan(side="long")` (research only, every rule mirrored; tests/test_backtest_long.py) |
| `features.py` | per-entry features (no look-ahead; tested by truncating the data at the entry) |
| `strategies.py`, `strategies_more.py` | pre-registered strategies + grids, placebos, random baseline |
| `stats.py`, `report.py`, `run.py` | cluster t, drawdown, Deflated Sharpe, walk-forward, splits, markdown |
| `failures.py`, `strategies_v2.py` | loss classifier + round-1 fixes (`--round1`); results and the improvement loop in `docs/FAILURES.md` |
| `strategies_v3.py`, `report_v3.py` | round 2 (`--round2`): breakdown entry types (limit / sell-stop / market), exit management, re-entry, funding budget |
| `strategies_v4.py`, `report_v4.py` | round 3 (`--round3`): live vetoes as blocked vs allowed partitions, premium buckets, OI flush / squeeze events |
| `placebo.py`, `trials_eff.py`, `report_v5.py` | round 4 (`--round4`): volatility/liquidity-matched same-hour placebo, effective-N Deflated Sharpe, corrections table ([failures/ROUND4.md](failures/ROUND4.md)) |
| `hist5m.py`, `trail5m.py` | round 4: 5-minute replay of the exits (breakeven / trailing), 5m bars cached in `data/hist5m.db` |

**Flags that change results** (defaults are the round-4 corrections; `--placebo=legacy --dsr=legacy` reproduces rounds 0–3 exactly):
- `--placebo=matched|legacy`: the same-hour placebo drawn from the donor's ATR% decile × 24h-volume bucket at that hour (nearest coins when the cell is empty), entered at the fill bar for resting entries, with the donor's funding budget; or the legacy random-coin draw.
- `--dsr=clustered|legacy`: effective N (trials whose daily R correlates > 0.7 are one family) and V[SR] across families with n ≥ 100; or raw N and V[SR] over every trial with n ≥ 10.

## Method

**Simulator** (every choice is the conservative one):
- Market entry at the signal bar's close minus slippage, taker fee.
- A limit sell fills only if a later bar trades *through* it (high > limit), maker fee.
- A sell-stop entry (round 2) fills when a later bar trades below its trigger, at the trigger or the gapped open, taker fee and 2× slippage.
- Stops fill at the stop, or at the bar open if it gapped past, with taker fee and 2× slippage. When a stop and a target are in the same bar, the stop comes first.
- Targets are resting limits: maker fee, a fraction of the initial size each.
- Breakeven and trailing update only after the bar closes.
- The time stop exits at the close.
- Funding is paid or received on every print held through, at the actual print time.
- **Fees:** Binance USDⓈ-M regular tier, taker 0.05% / maker 0.02% (checked 2026-09).
- **Slippage:** `20 bp / sqrt(24h volume in $M)`, clipped to 1–150 bp. That is 4.5 bp at $20M, 20 bp at $1M, 63 bp at $0.1M.
- **Stress run:** 1.5× fees, 2× slippage, and stop fills 10% of the way into the bar's overrun.
- **R:** net PnL divided by the planned risk (stop − entry)/entry.

**Squeeze:** a stop-out in a bar that ran ≥ 2R past entry. Hourly bars hardly ever gap, so stops land at the stop price. That is optimistic in real squeezes, and it is the reason for the stress run.

**Rules against fooling ourselves:**
- Every grid is fixed in `strategies.trials()` before any run. Every param set counts in the ledger, including the ones that produced 0 trades.
- One open trade per coin per trial. An entry is dropped when its exit window runs past the data, so censored trades never flatter the result.
- A $1M/24h volume floor applies at the signal. Liquidity is a split, not a filter.
- **Two placebos for every trial, with the donor trade's exits** (stop in ATR, targets in R, same time stop, breakeven, trail and funding budget):
  - (a) the same coin at random hours;
  - (b) the same hour on other coins. This one cancels the market regime. Since round 4 the coins come from the donor's own ATR% decile and 24h-volume bucket at that hour: signal coins are 2–3× as volatile as a random coin, so an unmatched placebo pays about twice the R of the same costs and inflates the net edge. On pump_fade and post_pump_decay that cost gap was nearly all of the legacy edge (see ROUND4).
- **Edge = strategy R − placebo R**, reported net and gross of costs. A random-entry baseline covers every coin.
- **Walk-forward:**
  - Each strategy's param set is picked on the first half of the window by IS avg R, then judged on the second half.
  - R by quarter shows whether the result lives in one stretch of time.
  - Since nothing else is fitted, OOS mostly measures regime stability.
- **Clustering and multiple testing:**
  - The t-stat is day-clustered (and coin-clustered), because trades on the same day are one market bet.
  - Deflated Sharpe (Bailey & López de Prado 2014) uses per-trade SR and the trades' skew and kurtosis. Since round 4, N is the effective number of independent trials (families of trials whose daily R correlates > 0.7) and V[SR] is taken across families with n ≥ 100, so clones and tiny partitions do not distort the hurdle. The raw-N DSR is reported next to it.
- **Verdict:**
  - **SURVIVES** needs all of: OOS > 0, IS > 0, beats both placebos, stress R > 0, t_day ≥ 2 and DSR ≥ 0.95.
  - **promising** means OOS > 0, IS > 0 and beats both placebos, but the result is not significant.

**Not testable here** (said plainly):
- **Unlocks, hype and news:** no unlock calendar in the data.
- **Liquidation exhaustion:** needs the WS stream.
- **CVD divergence and fake volume:** need aggTrades.
- **Spot-led vs perp-led:** only as a has-spot split.
- **Delisting:** only if hist.db carries `delist_ms`. It assumes the announcement came ≥ 3 days before, which the data cannot confirm.
- **Listing fade:** limited to listings inside the window, which gives a short hold.

## Results: 60 days, 2026-07-27 → 09-29

**Data:** 528 USDT coin perps from `data/hist.db` (520 trading, 8 settling), all with ≥ 96 hourly bars in the window; 369 have spot klines. 8 were delisted *inside* the window (ACX, HFT, VANRY, VIC on 08-07; ICX, SCRT, STORJ on 08-26; STG on 09-24) and are kept up to their delivery. The first build had 649 perps / 130 "delisted", of which 122 were zombie perps settled before the window (flat zero-volume archive bars); `histdata.trim_dead` now drops them.

**Trial ledger:** 36 param sets, V[SR] = 0.004, so the DSR hurdle is SR\* ≈ 0.15 per trade. Every figure is R after costs unless it says otherwise. The table below is round 0 with the legacy placebo and DSR; the round-4 corrections (matched placebo, effective N) are in [failures/ROUND4.md](failures/ROUND4.md).

| strategy (param set picked on IS) | n | coins | IS R | OOS R (n) | gross R | vs same-hour placebo, paired (t_day) | stress R | DSR | verdict |
|---|---|---|---|---|---|---|---|---|---|
| pump_fade (1.0 ATR, 12h) | 2211 | 507 | −0.09 | −0.06 (1114) | −0.02 | +0.10 (3.4) | −0.13 | 0.00 | fails (relative edge only) |
| level_fade (round, market, 24h) | 9157 | 527 | −0.16 | −0.18 (4696) | −0.11 | +0.02 (1.6) | −0.23 | 0.00 | fails |
| bounce_fade (24h ≤ −15%, 24h) | 198 | 107 | −0.03 | +0.15 (75) | +0.09 | +0.36 (3.6) | −0.01 | 0.08 | fails (relative edge only) |
| post_pump_decay (2 ATR, 12h) | 2857 | 516 | −0.06 | −0.11 (1435) | −0.04 | +0.07 (4.3) | −0.13 | 0.00 | fails (relative edge only) |
| listing_fade | 4 | 4 | | | | | | | insufficient: listings inside the window only |
| funding_hi (≥ 0.05%, EMA confirm) | 778 | 121 | −0.10 | −0.09 (355) | −0.06 | +0.05 (1.8) | −0.15 | 0.00 | fails |
| premium_hi (≥ 1%) | 51 | 34 | −0.03 | +0.05 (17) | +0.02 | +0.14 (0.6) | −0.02 | 0.16 | fails |
| squeeze_exhaustion | 6 | 4 | | | | | | | insufficient: 6 events in 60 days |
| laggard catch-up (corr ≥ 0.3) | 1089 | 347 | −0.22 | −0.06 (684) | −0.00 | −0.00 (0.1) | −0.22 | 0.00 | fails |
| pump_fade, Asia/late/weekend only | 1273 | 467 | −0.13 | −0.14 (634) | −0.06 | +0.09 (3.3) | −0.23 | 0.00 | fails; worse than all hours |
| level_fade, Asia/late/weekend only | 7603 | 527 | −0.15 | −0.20 (4014) | −0.09 | +0.03 (2.4) | −0.26 | 0.00 | fails |
| delisting (1–3 days before) | 8 | 8 | | | | | | | insufficient |

**Controls:**
- The pooled strategy trades (36k, deduplicated) average −0.21R.
- The same-coin placebo averages −0.20R, the same-hour placebo −0.25R, and random entries −0.23R.
- Random shorts lost about 0.11R *gross* in every quarter of this window. Alts drifted up and pumped (positive skew), and a short-only book pays for that.

**The 10-day cache (09-19 → 09-29) disagrees:**
- That cache covers a falling end of September. pump_fade, level_fade, post_pump_decay and premium_hi were all positive OOS (+0.1 to +0.3R).
- The first half of that same window was negative for all of them, and the per-quarter R tracks the same-hour placebo almost one for one.
- In other words, the market regime explains most of the result, not the trigger.

## What survives

**Nothing survives costs plus out-of-sample plus the multiple-testing haircut.** No strategy has a DSR above 0.16 or a day-clustered t above 1.1 on absolute R.

**Relative edge — withdrawn in round 4 ([failures/ROUND4.md](failures/ROUND4.md)).** Against a placebo matched on volatility and liquidity, pump_fade is +0.02R (t 0.9), post_pump_decay +0.00 and bounce_fade +0.14 (t 1.4). The legacy numbers below were mostly a cost gap: the random placebo coin had about 1/2 to 1/3 of the donor's ATR. Only pump_fade+discount keeps a relative edge (+0.14 net, +0.16 gross, t 3.1).

**Relative edge as first reported (legacy placebo, superseded):**
- pump_fade, post_pump_decay and bounce_fade beat shorting a random coin in the same hour by +0.07 to +0.36R.
- That edge holds in both halves, with a day-clustered t of 3.4–4.3. The trigger adds something, but less than what the market drift takes away.
- It would pay only with a long hedge (the time placebo is the hedge's P&L), or in a falling regime.

**Closest to live:**
- **bounce_fade with a 24h move ≤ −15%:** a continuation short of a coin already down 15%, entered on a rejected EMA20 bounce after a lower high.
  - +0.09 gross, +0.04 net, +0.15 OOS, beats its placebo by +0.36R (t 3.6).
  - But it has only 198 trades, a day-clustered t of 0.9, a DSR of 0.08, and −0.01R under stress. Its looser −8% sibling fails.
  - It is a candidate for **shadow logging** (docs/LADDER.md-style), not for size.
- **Things that do NOT work:**
  - laggard catch-up: no edge vs placebo at all.
  - funding_hi as a trigger, which matches PLAYBOOK "never a trigger on its own".
  - exp_level_fade: the most trades and the worst coin-clustered t (−19). Sweeps of 24h highs and round numbers do not reverse enough to pay a 24h hold.
  - waiting for limit fills above the level: the fill is adversely selected.

## Which features matter (pooled 60d; the same-hour placebo is shown so drift/cost effects are not mistaken for edge)

| feature | finding |
|---|---|
| liquidity (24h vol) | Monotone. <$2M: −0.30R, $2–5M: −0.22, $5–20M: −0.16, ≥$20M: −0.12. The placebo shows the same slope, so this is **cost**, not signal. pump_fade at $20–100M is 0.00R. Keep a floor near $5–20M. |
| volatility (ATR% tercile) | High ATR: −0.12R, low: −0.27. Mechanical: fixed costs are smaller in R when the stop is wide. Avoid tight-stop fades on quiet coins. |
| session / hour | Weekend −0.10R vs weekday −0.25 (placebo −0.17 vs −0.28). late_wknd is the only positive cell (+0.07, n 902). The pre-registered **Asia/late/weekend filter made pump_fade worse**, so PATTERNS.md's session finding did not repeat. |
| listing age / spot | Age <30d has no usable n. No-spot coins do slightly better (−0.17 vs −0.23; pump_fade −0.04 vs −0.10), but the placebo has the same tilt. Weak. |
| synchronized vs idiosyncratic | Fading an **idiosyncratic** 4h move beats fading a BTC/sector-synchronized one: −0.16 vs −0.27 pooled, and pump_fade −0.05 vs −0.20. The placebo gap is smaller, so this is a real but modest filter. |
| BTC regime | BTC up ≥ 2% over 24h is the worst bucket (−0.36R). This supports the "no new shorts on a BTC up-impulse" veto. |
| who is long (top-trader vs global L/S) | Flat: whales-longer −0.19, retail-longer −0.22, aligned −0.21. No evidence that whale/retail positioning helps at 1h. |
| taker ratio | Buy-dominant (≥ 1.1) −0.24 vs balanced −0.14. Taker flow continues (TAPE.md), so treat heavy buying as a veto. |
| trend (efficiency ratio) | A clean one-direction run (ER ≥ 0.45) is the worst bucket (−0.34R). Don't fade a smooth trend. |
| distance to the 24h high | Shorting within 1% of the high is the worst bucket (−0.28R); ≥ 15% below it is ≈ 0. Fading *at* the high loses to momentum, and continuation after a drop is the better short (see bounce_fade). |
| funding / premium / OI change | Small effects. Negative-funding entries were *not* worse (−0.12R). |

## Caveats
- One 2-month window is essentially one regime (an alt-up drift), and hourly bars hide intrabar squeezes: stops almost never gap on 1h. The stress run is the honest cost bound.
- **Walk-forward:** the IS/OOS split has no fitting beyond picking 1 of ≤ 4 param sets, so OOS is a regime check. Rerun the same ledger on a new window before trusting any cell.
- **Pooled splits** are descriptive, with 25 features × ~4 buckets. Nothing there is a finding without a same-hour-placebo contrast and a repeat on new data.
- **Order of changes:**
  - First run (10d): pre-registered strategies and grids.
  - After it: two controls added (the same-hour placebo and the paired edge), a funding-index fix found by the look-ahead test, and a listing-date guard.
  - The grids themselves never changed.

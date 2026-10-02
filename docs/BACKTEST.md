# Strategy backtest (research 2026-09-29)

`src/shortbot/backtest/` replays every strategy the docs describe on cached hourly history, with costs, placebos,
walk-forward and a multiple-testing haircut. Read-only: it never touches the exchange or the journal.

    .venv/bin/python -m shortbot.backtest.run --days60     # data/hist.db -> data/research/<UTC date>-<sha7>/bt60/; drop --days60 for the 10d cache

| file | what |
|---|---|
| `sim.py` | event-driven hourly simulator for one trade with a plan: a short, or a long with `Plan(side="long")` (research only, every rule mirrored; tests/test_backtest_long.py) |
| `features.py` | per-entry features (no look-ahead; tested by truncating the data at the entry) |
| `strategies.py`, `strategies_more.py` | pre-registered strategies + grids, placebos, random baseline |
| `stats.py`, `report.py`, `run.py` | cluster t, drawdown, Deflated Sharpe, walk-forward, splits, markdown |
| `../cluster_se.py`, `register_rows.py`, `../trial_register.py` | #31: the t of the pooled mean (day, coin, two-way); what each study records in the trial register; the register itself |
| `failures.py`, `strategies_v2.py` | loss classifier + round-1 fixes (`--round1`); results and the improvement loop in `docs/FAILURES.md` |
| `strategies_v3.py`, `report_v3.py` | round 2 (`--round2`): breakdown entry types (limit / sell-stop / market), exit management, re-entry, funding budget |
| `strategies_v4.py`, `report_v4.py` | round 3 (`--round3`): live vetoes as blocked vs allowed partitions, premium buckets, OI flush / squeeze events |
| `placebo.py`, `trials_eff.py`, `report_v5.py` | round 4 (`--round4`): volatility/liquidity-matched same-hour placebo, effective-N Deflated Sharpe, corrections table ([failures/ROUND4.md](failures/ROUND4.md)) |
| `hist5m.py`, `trail5m.py` | round 4: 5-minute replay of the exits (breakeven / trailing), 5m bars cached in `data/hist5m.db` |

**Flags that change results** (defaults are the round-4 corrections; `--placebo=legacy --dsr=legacy` reproduces rounds 0–3 exactly):
- `--placebo=matched|legacy`: the same-hour placebo drawn from the donor's ATR% decile × 24h-volume bucket at that hour (nearest coins when the cell is empty), entered at the fill bar for resting entries, with the donor's funding budget; or the legacy random-coin draw.
- `--dsr=clustered|legacy`: effective N (trials whose daily R correlates > 0.7 are one family) and V[SR] across families with n ≥ 100; or raw N and V[SR] over every trial with n ≥ 10.

## Provenance (`provenance.py`)

Every run writes `report.md`, `results.json`, `trades.json` and `provenance.json` to `data/research/<UTC date>-<sha7>[-dirty]/<label>/` (`--out DIR` to choose, `--no-save` to print only). An existing non-empty dir is never overwritten: `-2`, `-3`... `data/` is the journal's directory (`SHORTBOT_DB`), so the Docker guardian writes to `/app/data/research`. `refresh.py`, `hedge_report.py`, `trail5m.py`, `carry_regime.py`, `funding_events.py` and `lifecycle_report.py` stamp their outputs the same way. Cite that dir in a learning's evidence, never a scratchpad or `/tmp`.

`provenance.json` (also under `results.json` → `provenance`) records:
- **Code:** the git sha and a dirty flag (uncommitted or untracked changes under `src/` or `pyproject.toml`), and `code_sha`, a hash of every imported module, which still identifies the code in an installed copy without git.
- **Data:** each input's fingerprint: per table the rows, the min/max of its time column and the distinct symbols (`id`, about 0.3 s on hist.db), plus the file's sha256 (about 0.1 s), which also catches values overwritten in place. `changed_during_load` means the DB changed while the run read it.
- **Window and config:** the window (t0/t1, IS/OOS mid, coins), every flag, the ledger, costs and stress costs, and the placebo seeds (`crc32(trial name) % 10000`). Python, ccxt and numpy versions.

    .venv/bin/python -m shortbot.provenance verify data/research/<dir>/bt60_r3   # [] = same code and inputs
    .venv/bin/python -m shortbot.provenance rerun data/research/<dir>/bt60_r3    # the command, window pinned

**The hist.db window.**
- hist.db is extended in place every week and never trimmed: `--days60` loads all of it, so the "60-day" run covers 67, 74... days after each refresh and its IS/OOS mid moves. `--t0` / `--t1` (epoch ms or UTC date) pin a window, and `rerun` adds the recorded one.
- A pinned rerun on a later build is close, not exact. Archive files overwrite the REST-tail hours with archive values. A coin settled before the new build's start is purged, so an old window loses its delisted coins: survivorship bias creeps back into old windows. `verify` names the drift.
- `refresh.py` fingerprints hist.db before and after each rebuild (`refresh.json` → `hist_db`), so the previous build's fingerprint sits next to the new results.
- **Proposal (owner's disk, not done):** snapshot hist.db before each rebuild with `shortbot backup --db data/hist.db` (a verified `.db.gz` plus a manifest of row counts and sha256s, see deploy/DEPLOY.md), and keep the copies a learning cites. Without one, an exact rerun of a past window needs the `.db.gz` the weekly backup job still holds.

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
  Checked against the desk's own fills: journal stats `fill_quality` (each fill vs the mainnet price at it, next to this model; `fill_report.py`). `fill_report.calibrated_costs(journal)` refits the 20 bp from them, and keeps it until 30 trades over 20 days. Demo fills calibrate the demo venue, not live.
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
- **Clustering and multiple testing** (#31; tables written before it say "pre-#31 estimator"):
  - Every t is the t of the reported pooled mean (avg R), clustered because trades on one day are one market bet and trades on one coin share the coin (`cluster_se.py`):
    - `t_day`, `t_coin`: CV3 jackknife variance floored at the iid one, t(G − 1), as in `research_stats` and the registry's forward test (Cameron & Miller 2015 §VI).
    - `t_2way` (day × coin): Cameron, Gelbach & Miller (2011, NBER t0327 §2.2 eq. 2.15) V = V_day + V_coin − V_day∩coin, each with the factor G/(G − 1) of its own cluster count (their Stata-style choice, p.8), df min(G, H) − 1 (§3.1). CGM p.9: if V is negative, use the larger one-way variance. Here that floor always applies, so `t_2way` is never above either one-way t.
    - `n_eff` = s² / V_2way: the iid sample with the same precision (Kish).
    - Before #31 the t was over equal-weighted cluster means: a different number than the avg R next to it. On the round-4 picks: pump_fade+discount 3.19 → 2.14 (avg R +0.12), laggard −2.14 → −1.24 (n_eff 71 of 1,089 trades).
  - Deflated Sharpe (Bailey & López de Prado 2014, eq. 2) uses per-trade SR and the trades' skew and kurtosis.
    - **T** is the book's `n_eff`, not its trade count. PSR assumes IID or stationary-ergodic observations (Bailey & López de Prado 2012 §1–2), and same-day trades are neither.
    - **N** is the effective number of independent trials (their App. A.3: N, not the M trials run). Since round 4: families of trials whose daily R correlates > 0.7, and V[SR] across families with n ≥ 100, so clones and tiny partitions do not distort the hurdle.
    - Since #31 the families span the whole search: this run's trials and the feature-split cells it shows, plus every other trial in the **trial register** (below). A register trial without a daily series counts as its own family (conservative).
    - Reported: `dsr_legacy`, `dsr_rawN`, `dsr_effN` (this run only) and `dsr_register`, which the verdicts use.
- **Trial register** (`trial_register.py`, `data/research/trials.db`): one append-only table of every trial any study ran.
  - Writers: backtest trials and split cells, hedge variants, trail5m exits, rungs, card tests, carry / funding / xs studies, lifecycle grid, `study patterns|laggard|ladder`, `registry seed`.
  - Reruns of a spec (study, family, params) append rows but count once. Placebo / random groups are recorded as controls and never counted.
  - `--no-save` runs record too: a look at the data is a trial.
  - The journal's `pattern_trials` and the ladder's `trial_ledger` are legacy. Copy them in once with `python -m shortbot.trial_register migrate` (reads the journal only). After that `summary` shows the counts.
  - A run reads the register as of a row id (`results.json` → `register.max_id`), and `rerun` pins it (`--trials-asof`).
  - The guardian and the MCP never need the file: a missing register reads as empty. `shortbot backup` copies it with the journal once it exists.
  - A test (`tests/test_register_studies.py`) fails when a new `backtest/*.py` entry point does not record its trials.
- **Verdict:**
  - **SURVIVES** needs all of: OOS > 0, IS > 0, beats both placebos, stress R > 0, t_2way ≥ 2 and DSR ≥ 0.95 (register-wide N). Before #31: t_day ≥ 2 and the run's own N.
  - **promising** means OOS > 0, IS > 0 and beats both placebos, but the result is not significant.

**Not testable here** (said plainly):
- **Unlocks, hype and news:** no unlock calendar in the data.
- **Liquidation exhaustion:** needs the WS stream.
- **CVD divergence and fake volume:** need aggTrades.
- **Spot-led vs perp-led:** only as a has-spot split.
- **Delisting:** only if hist.db carries `delist_ms`. It assumes the announcement came ≥ 3 days before, which the data cannot confirm.
- **Listing fade:** limited to listings inside the window, which gives a short hold.

## Results: 60 days, 2026-07-27 → 09-29

The t's and DSRs in this and the following result tables are from the **pre-#31 estimator** (see "Clustering and multiple testing").

**Data:** 528 USDT coin perps from `data/hist.db` (520 trading, 8 settling), all with ≥ 96 hourly bars in the window; 369 have spot klines. 8 were delisted *inside* the window (ACX, HFT, VANRY, VIC on 08-07; ICX, SCRT, STORJ on 08-26; STG on 09-24) and are kept up to their delivery. The first build had 649 perps / 130 "delisted", of which 122 were zombie perps settled before the window (flat zero-volume archive bars); `histdata.trim_dead` now drops them.

**Missing coins (fixed in #73).** Until 2026-10-02 `histdata._is_coin_perp` matched names against `[A-Z0-9]+USDT`, so coin perps with Chinese names never entered hist.db or daily.db: 币安人生USDT (listed 2025-10-20), 我踏马来了USDT (2026-01-21), 龙虾USDT (2026-03-11), 牛来USDT (2026-08-30) and 哈基米USDT (2026-09-06). The live scanner ranks them (牛来USDT was in 36 of the 122 scans logged 09-27 → 10-01). Every result in this file ran without them: rounds 0–4, the long sleeve (#18), the positioning cards (#16), the 5-minute exit replay, and the daily.db studies (carry regime). Add them with the weekly rebuild, `.venv/bin/python -m shortbot.histdata build --days 67` (on 2026-10-02 that reaches back to the 07-27 start), and `.venv/bin/python -m shortbot.histdaily build --since 2020-01 --symbols 币安人生USDT 我踏马来了USDT 龙虾USDT 牛来USDT 哈基米USDT`.

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

## Long sleeve (research, 2026-10-02, task #18)
`run --days60 --long` runs the four pre-registered long strategies from the library (`strategies_long.py`: 12 param sets, effective N 6) through the same simulator (`Plan(side="long")`), the mirrored matched placebos, stress and DSR. Window 2026-07-27 → 09-29.

| strategy (picked on IS) | n | coins | avg R | OOS R | vs same-hour placebo, paired (t_day) | stress R | verdict |
|---|---|---|---|---|---|---|---|
| flush_reclaim (6% drop, all coins) | 656 | 248 | +0.18 | +0.37 | −0.01 (0.1) | +0.14 | fails |
| oversold_bounce (7% drop) | 819 | 279 | −0.06 | −0.09 | −0.03 (−0.8) | −0.12 | fails |
| capitulation (−40%/7d, 168h) | 59 | 49 | +0.03 | −0.08 | −0.19 (−0.4) | +0.01 | fails |
| premium_discount (z ≤ −3) | 4,174 | 477 | +0.01 | +0.00 | **+0.03 (2.6)** | −0.08 | promising |

Pooled long trades: +0.01R. Same-coin placebo: +0.02R. Same-hour placebo: +0.00R. Random shorts: −0.23R.

**Read:**
- In this up-drifting window, simply being long was worth about +0.2R over being short.
- None of the long triggers adds anything beyond that direction.
- The flush-reclaim's +0.18R is the market's: the same-hour placebo made +0.19R.
- The liquid-only variants (≥ $100M) were worse (paired −0.24/−0.32R, t ≈ −2). That contradicts the card's "majors only" idea in this window.

**Next:**
- Shadow-log the long side of premium_discount, the only variant worth it.
- Retest all four on a new window before any long is considered.

### Round 2 (task #32, `strategies_long2.py`, `run --days60 --long2`)
Hourly versions of the two daily long edges (docs/REGIME_STUDIES.md §5): 6 pre-registered trials, effective N 3.

| strategy (picked on IS) | n | avg R | OOS R | vs same-hour placebo, paired (t_day) | stress R | verdict |
|---|---|---|---|---|---|---|
| breakout_continuation (168h high, 3× volume, 72h) | 748 | +0.04 | −0.00 | −0.07 (−1.4); the 72h-high variants −0.08/−0.09 (t −2.1) | −0.00 | fails |
| rs_leader_pullback (top 10 by 168h residual, EMA20 dip) | 327 | +0.01 | −0.03 | −0.02 (−0.2) | −0.03 | fails |

**Read:**
- Buying hourly volume breakouts is *worse* than buying a random coin in the same hour, which fits the short book's pump fades.
- The daily edges don't survive translation to hourly triggers. Longs act on daily closes only (docs/LONG_PLAYBOOK.md).

## Positioning cards (research, 2026-10-02, task #16)
`run --days60 --flow` runs five pre-registered strategies from the library (`strategies_flow.py`: 10 param sets, effective N 7). They use the hourly OI, long/short ratios and taker ratio from data.binance.vision metrics.

| strategy (picked on IS) | n | avg R | stress R | vs same-hour placebo, paired (t_day; IS/OOS) | DSR | verdict |
|---|---|---|---|---|---|---|
| long_trap_short (OI +5%, price −3%, funding > 0) | 244 | −0.01 | −0.06 | **+0.10 (1.7; 0.10/0.11)** | 0.08 | fails (relative edge) |
| crowded_unwind_short (OI +40%/7d) | 63 | −0.24 | −0.27 | −0.28 (−1.8) | 0.00 | fails |
| ls_contrarian_short (retail ratio z ≥ 2) | 53 | −0.19 | −0.27 | −0.06 (−0.9) | 0.02 | fails |
| squeeze_fuel_long (OI +10%, price −3%, funding < 0, reclaim) | 49 | +0.34 | +0.31 | +0.13 (0.3) | 0.98 | promising |
| ls_contrarian_long (retail ratio z ≤ −3) | 273 | +0.03 | −0.06 | +0.01 (−0.6) | 0.21 | promising (no real edge) |

Pooled: −0.01R. Same-coin placebo: −0.06R. Same-hour placebo: −0.01R. Random shorts: −0.23R.

**Read:**
- The positioning triggers add little beyond direction.
- The long trap is the only short with a consistent relative edge: it beats shorting a matched coin that hour in both halves. It is still not an absolute one.
- The squeeze-fuel long is the best long found so far: positive in both halves and under stress, collecting +0.85% funding per trade. But with only 49 trades and a weak edge over its placebo it is a shadow-log candidate, not a setup.

## Caveats
- One 2-month window is essentially one regime (an alt-up drift), and hourly bars hide intrabar squeezes: stops almost never gap on 1h. The stress run is the honest cost bound.
- **Walk-forward:** the IS/OOS split has no fitting beyond picking 1 of ≤ 4 param sets, so OOS is a regime check. Rerun the same ledger on a new window before trusting any cell.
- **Pooled splits** are descriptive, with 25 features × ~4 buckets. Nothing there is a finding without a same-hour-placebo contrast and a repeat on new data.
- **Order of changes:**
  - First run (10d): pre-registered strategies and grids.
  - After it: two controls added (the same-hour placebo and the paired edge), a funding-index fix found by the look-ahead test, and a listing-date guard.
  - The grids themselves never changed.

## Live-pipeline replay (task #29, stage 1)
The strategies above are hand-written proxies; none of them runs `signals/`, `tape.py` or `setups.py`. The replay runs **the live code itself** on history: `scanner.scan_shorts` gets a `ReplayExchange` pinned to a past moment, and everything above the exchange (DataHub, every signal, the tape classifier, the setup detectors, the market map and the candidate logging) is the code the desk runs.

| file | what |
|---|---|
| `clock.py` | market clock (ContextVar, wall clock by default). signals/, market/ and scanner.py read "now" only through it; `tests/test_clock.py` fails on any other wall-clock read there unless the line says `# wall-clock` |
| `replay_bars.py` | REST-shaped klines with the forming bar last, the rolling 24h ticker (openTime = floor_minute(now) − 24h), Binance's predicted-funding formula |
| `replay_store.py`, `replay_fetch.py` | scratch cache: 1m perp klines for every coin perp, 1m mark / premium / spot and 5m metrics for the coins near the top, settled funding, exchangeInfo listing facts |
| `replay_exchange.py` | the exchange surface the pipeline calls, served as of the clock (SQL-bounded: a bar is read only once it has closed) |
| `replay_world.py` | read-only journal snapshot, the world copy the replayed DataHub reads guardian feeds from, risk limits in force at t (decisions timeline), blocked network feeds |
| `replay_compare.py`, `replay_verify.py` | replay every logged scan and compare it with what the desk logged |

    .venv/bin/python -m shortbot.backtest.replay_verify --journal data/shortbot.db --work $SCRATCH/replay-cache \
        --since 2026-09-27 --until 2026-10-02 --hist data/hist.db --daily data/daily.db --fetch --out $SCRATCH/replay-out/run

**Point in time.** The universe at t is every coin perp with onboardDate ≤ t < deliveryDate (on the demo list, `--universe demo`), ranked by the rolling 24h change rebuilt from 1m bars. The liquidity floor is the one in force at t (20M, 5M from 2026-09-29T18:15 under lev10_risk3+liq_tiers). Liquidations, Square snapshots, unlocks (by `added_at`) and macro events come from the guardian's recordings, bounded by the clock. A test poisons every row after t and requires an identical replayed scan.

**Not replayable** (no point-in-time history): order book, news, listing announcements, CoinGecko trending, Binance hype (blocked; `--oracle` feeds trending/hype from the log), product tags / supply facts / the demo account's coin list (today's snapshot), delisting announcement times (`--lead-days` before deliveryDate). Predicted funding is estimated from 1m premium klines (Binance samples every 5 s).

**Sources** (checked 2026-10-02): kline intervals, file layout and next-day publication in github.com/binance/binance-public-data; the S3 listing (`s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix=data/futures/um/daily/`) keeps settled perps (STGUSDT 1m from 2022-08-25 past its 2026-09-24 delivery); funding formula in binance.com/en/support/faq/360033525031; `/fapi/v1/exchangeInfo` still lists 131 SETTLING perps with deliveryDate; `/fapi/v1/ticker/24hr` spans 24 h with a minute-aligned openTime. The developers.binance.com REST pages render client-side and could not be read with our tools; the REST facts above were checked on the public endpoints.

**Verification (2026-10-02, 122 logged scans 09-27 → 10-01, 2,964 candidate rows, code 9a3e3c7; rerun with the command above).** A row matches when the replay evaluated the same coin and agrees on tradable, setups and tape state. Logged scans before 2026-09-29T19:37 ran code that is not in git (the "OI < $xM" and negative-funding vetoes), so decisions are judged from there on; 3 days, so the day-clustered intervals are descriptive only.

| since 09-29T19:37 (52 scans, 1,243 rows) | value |
|---|---|
| **row match** | **90.2%** (95% CI 81–95%) |
| coin evaluated by the replay | 97.7% |
| tradable / setups / tape state / veto reasons agree (rows in both) | 98.4% / 98.8% / 94.3% / 91.9% |
| universe recall / precision per scan | 97.9% / 97.4% |

All 122 scans: row match 84.3%, universe recall 97.0% (ranked scans 96.5%, "now" scans 98.8% with their threshold inferred from the logged count). Remaining differences are data, not code: spot-led-rally, volume-heat and Square-mention vetoes near their thresholds (1m closes instead of live seconds), the predicted funding estimate (51% within 1%), five coins that left the demo list since, and rotation flags (the replay rebuilds fewer maps than the guardian). Two fixes the comparison found: the 4 h / 1 h funding formula (÷ 8/N) and coin perps with Chinese names, which `histdata._is_coin_perp`'s ASCII pattern also drops from hist.db.

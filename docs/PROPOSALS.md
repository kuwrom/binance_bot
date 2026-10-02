# Proposals with their evidence (2026-10-02)

> **Statistics note (#31, 2026-10-02):** the clustered t-statistics and Deflated Sharpe ratios quoted here come from the **pre-#31 estimator**: a t over equal-weighted day (or coin / hour) means, not the t of the pooled mean reported beside it, and a DSR with T = the trade count and N = one run's trials. They are kept as recorded. A rerun reports the t of the pooled mean (day, coin and two-way) and a DSR on the effective sample with N from the trial register: docs/BACKTEST.md "Clustering and multiple testing".

These are designs, not code. P1a and P2 now carry research-derived answers, not open questions. Evidence lives in [INDUSTRY.md](INDUSTRY.md) §5 and the library cards.

## P1. Exit and venue hygiene for open shorts (task #14)
**Who decides:** binance-bot-99, who owns guardian / entries / trading. The human signs off on the defaults.

**Why:** the 10-11 Oct 2025 cascade (learning #35).
- $19.3bn was liquidated in 24h, 87% of it longs.
- Binance auto-deleveraging (ADL) closed large *profitable* shorts, ranked by PnL% × effective leverage.
- Binance marked USDe at $0.62-0.65 from its own order book and liquidated hedged accounts.
- Market makers were locked out for ~2h.
- ATOM printed $0.001, TON −80%, WLD −70%.
- Resting bids far below market filled. Hyperliquid's HLP made +$40m that weekend, ~41% of its lifetime profit across two such events.

**Today** (code map, 2026-10-02):
- Take-profits were not resting orders: the guardian's 60-second pass bought back at MARKET on `markPrice`, and a wick between passes was missed. Since #65, automatic targets rest (P1a, below).
- Resting on the exchange: the entry SELL limit, a reduce-only STOP_MARKET pre-arm, one closePosition STOP_MARKET, temporary bridge stops and, since #65, the automatic targets as reduce-only BUY limits (`sb-t…`).
- There is no ADL monitoring, no check of the margin asset or multi-asset mode, and no margin-ratio or liquidation-distance check once a position is open. The liquidation checks run only at entry: risk.py:37-47 and trading_open.py:171-173, the latter a warning only.

### P1a. Resting reduce-only take-profit orders: research result (2026-10-02, `backtest/rungs.py`)
**Method:** a pre-registered grid of 3 depth sets × 3 sizes, tested on three samples, with the uplift paired per signal and a day-clustered t:
- all 34,800 signals of the 60-day hourly ledger;
- 2,465 random-entry shorts, as a control;
- 5,281 daily top-gainer fades from 2020 to 2026, which include October 2025.

**Baselines:**
- **"targets":** the plan's targets execute.
- **"runner":** targets only alert, as in live plans with `auto.targets` off.

| variant (50% of size) | hourly strategies: uplift R (t) | random control | daily fades 2020-26 |
|---|---|---|---|
| rungs carved out of the targets, 4/8/12% | −0.005 (−1.9) | −0.018 (−3.4) | −0.011 (−3.2) |
| rungs carved out of the targets, 6/12/20% | −0.008 (−2.2) | −0.022 (−3.2) | −0.004 (−3.1) |
| rungs carved out of the targets, 10/20/30% | −0.010 (−2.4) | −0.026 (−3.5) | −0.000 (−0.7) |
| rungs added to a runner, 4/8/12% | +0.008 (3.6) | +0.010 (2.9) | −0.003 (−0.2) |
| rungs added to a runner, 6/12/20% | +0.005 (3.9) | +0.006 (2.9) | +0.004 (0.7) |
| rungs added to a runner, 10/20/30% | +0.003 (2.2) | +0.002 (1.6) | +0.008 (1.7) |

Uplift grows roughly linearly with size (30 / 50 / 70%). The 6/12/20% rungs fill on 19% of hourly trades and 75% of daily fades.

**Evidence-based rule:**
1. **Never carve rungs out of automatic targets.** It lost R in every sample, because the carved slice gives up a target fill and often never reaches the deeper rung.
2. **Rest rungs only on the part of a position with no automatic target:** the runner after the last target, or plans whose targets only alert. Use 6/12/20% below entry (positive in all three samples; 4/8/12% failed on the daily data) at up to 70% of that remainder.
   - The expected gain is small, about +0.005-0.01R per trade. It is a free option on wicks, not an edge.
3. **The bigger gap is execution, not depth.** The backtest credits targets as resting limits that fill on any last-price touch. Live, the guardian closes targets at market when its 60-second mark-price check sees them, and mark price smooths out last-price wicks.
   - Rule: make automatic targets resting reduce-only limit orders at the target prices. This is the execution the backtest already assumes, and it adds no carve cost.
   - **Built (#65, `targets.py`, `targets_sync.py`).** Each automatic target rests as a reduce-only GTC BUY LIMIT at its price (client id `sb-t<trade>-…`), sized by the plan's cumulative fractions of the original size, so manual partials and adds count. GTC, not post-only: a target placed when the book is already through it fills at once at its price or better instead of being rejected. Plain LIMITs use the 200-open-orders budget per symbol, not the 10 conditional slots (stop, bridges, pre-arms).
   - Kept in sync by the guardian every pass (fills settled, missing ones placed, wrong ones cancelled, orphans cancelled) and right after a partial close, an add, a widen, a stop move or `update_plan`. A partial close or a widen's cut cancels the targets first, so none fills with it. Flat cancels them, whatever Binance does on its own. A new short on the symbol (`open_short`, a limit-entry fill) cancels any target of another trade first.
   - The market fallback stays: a target the mark crossed without its limit having filled (gone from the book, refused, part-filled) is closed at market by the guardian, idempotently (`of_original`), as before.
   - **Measured:** each fill is a `target_fill` decision (target, order, price, qty, time, slip in bps) plus the row in `target_orders` (status history, why it left the book) and a mainnet reference (`fill_refs`, type `target`). Orders Binance took away are `target_gone` decisions.

**Still unverified** (Binance's docs leave them open; the code is safe either way: it re-lists the book every pass and cancels on flat):
- whether an untriggered conditional reduce-only order (a stop swap's bridge, a pre-arm) takes part in the reduce-only competition and cancels targets;
- whether Binance expires reduce-only limits at flat (Binance staff, dev.binance.vision/t/802, say EXPIRED), and what a partly filled one reads;
- what a cut that is not an order (a partial ADL) does to them;
- the ADL endpoint format.

**Demo drill (#65, owner-approved, run by the coordinator):** a minimum-size short on a liquid coin with `auto.targets` on and two targets a little below the price.
1. Wait for a guardian pass: `sqlite3 -readonly data/shortbot.db "select id,target,qty,status,order_id,history from target_orders order by id desc limit 5"`. Expect two `resting` rows; the book shows two reduce-only BUY limits `sb-t<trade>-…`.
2. `move_stop` once (a bridge is placed and cancelled). Same query: are the rows still `resting`? A `target_gone` decision (`select ts,snapshot from decisions where action='target_gone'`) means the bridge's competition took them; the next pass places them again.
3. Let one target fill (or move a target just under the price with `update_plan`): a `target_fill` decision shows price, qty, time and slip; for a partial fill, `filled` < `qty` while it stays `resting`.
4. Close the rest by hand on Binance (not through the desk), and look at the rows before the next pass: `history[-1].ex` is Binance's own status at flat (`expired` or `canceled`), or the row is still `resting` and the next pass cancels it (`why` = "flat: the position is closed").

### P1b. ADL watch: built (#64, `guardian_margin.py`, facts and sources in `margin.py`)
- **Endpoint** (Binance's SDK carries the doc text; JKorf/Binance.Net #1107 for the shape): `GET /fapi/v1/adlQuantile`, weight 5, values update every 30 s, 0-4 = queue position low to high. One-way Mode answers LONG, SHORT and BOTH (ours: BOTH). Without `symbol` a list (`[]` when flat, demo read 2026-10-02), with one a single object. A populated answer is still unverified on demo: the parser is tolerant, anything unexpected is logged as an ERROR with the raw answer, and the first non-empty answer is kept in kv `adl_sample`.
- **Cadence:** one read per watch pass (60 s) for every short. At weight 5 a minute, the 5 min / 60 s switching wasn't worth its code.
- **Alert:** quantile 4 (the top bucket) sends `adl_risk` (routine, 1 h cooldown) with effective leverage and "consider a partial take-profit". 15 passes in a row at 4 send one urgent `adl_risk_persistent`, and its end `adl_risk_cleared`.
  - Why only the top bucket: ranking = PnL% x effective leverage, so winning shorts sit high as a rule. For a short, ADL is an exit at the bankrupt long's price plus cancelled orders on the symbol: lost upside, not a loss.
- **Failure:** an answer without a held short's quantile is a watch failure (`position_watch_error`, the watch streak, #57).
- **Effective leverage** = |notional| / isolatedMargin, logged on a change and every 15 min, beside positionRisk v3's own `adl` field (compare them on demo). Binance's isolatedMargin is already wallet + uPnL, so the earlier "(isolated margin + uPnL)" counted uPnL twice.
- **Not built:** the `plan.auto.adl` partial close (alert-only by decision).
- **Exits:** a close Binance made itself is journalled with exit_reason `adl: ...` or `liquidation: ...`. Its exit order is listed by `GET /fapi/v1/forceOrders` (autoCloseType ADL / LIQUIDATION, weight 20).

### P1c. Margin asset, mode and buffer checks: built (#64)
- **Account:** the monitor job `margin_mode` (5 min) reads `GET /fapi/v1/multiAssetsMargin` (weight 30). If on, it sends urgent `multi_assets_mode`. Isolated margin is refused under it (-4168), so every entry already fails loudly; no REDUCING trip.
  - Non-USDT balances in single-asset mode are no margin for USDT perps, so the asset check is per position: `marginAsset` not USDT/USDC sends urgent `margin_asset`.
- **Position** (protective positions pass, after the stops):
  - crossed (ccxt: isolatedMargin "0") sends urgent `margin_not_isolated`;
  - unreadable fields send urgent `margin_check_error`.
- **`_prepare`:** -4047/-4067 (open orders block the switch; demo answers -4067 whenever an order rests) no longer counts as proof of isolated.
  - `GET /fapi/v1/symbolConfig` decides: ISOLATED proceeds, CROSSED refuses before any order.
  - Unreadable: proceeds only when this desk confirmed the symbol isolated within 24 h (kv `isolated_ok:<symbol>`, a split entry's earlier leg), else refuses.
- **Margin buffer:**
  - isolatedMargin < 2x maintMargin sends urgent `margin_thin` (alert only; it blocks nothing, see `guardian_margin.py`).
  - A resting stop past liquidation minus `liq_buffer_pct` is tightened there (`Trader.cap_stop`). This is the cap `ensure_stop` restores at, shared and rounded down to the tick. It tightens only, never loosens, and the journal's plan stop stays. Price already past the cap closes the short, as the restore does. The alert is urgent `plan_stop_unreachable`, once per cooldown.

## P2. Per-setup drawdown ladder ("pods"): research result (task #21, 2026-10-02)
**Sources:**
- Literature: scratchpad r7 → summarised here. [V] means read from the source this session.
- Own numbers: `backtest/drawdown.py` on the selected trades of the 60-day ledger, per-trade R block-bootstrapped with its fat tails and serial correlation.

**What the data says about our trade R:**
- Volatility is σ = 0.63-1.08R per trade.
- Lag-1 autocorrelation is positive, 0.11 to 0.30: losing streaks cluster.
- Kaminski & Lo [V]: stop rules lower expected return under a random walk, but add value when outcomes persist. Our autocorrelation meets that condition, so a de-risking ladder is justified.

**Minimum edge to protect: μ = +0.10R per trade.** That is a per-trade Sharpe of about 0.13, or about 1.0 a year at about 50 trades per setup, matching the desk's net Sharpe target of 0.5-1.0.

**Q1, thresholds.** A threshold at quantile q of the worst drawdown over the next 100 trades, for a setup that truly earns +0.10R, wrongly hits that setup with probability 1 − q.

| setup (σ, lag-1) | 80% quantile = halve | 95% = stop | 250-trade 95% |
|---|---|---|---|
| pump_fade (0.74, 0.11) | 8.6R | 12.3R | 16.0R |
| post_pump_decay (0.63, 0.17) | 7.7R | 10.8R | 14.2R |
| funding_hi (0.74, 0.09) | 8.2R | 11.5R | 15.4R |
| bounce_fade (1.08, 0.03) | 11.1R | 15.7R | 20.8R |
| level_fade (0.81, 0.24) | 11.3R | 16.2R | 22.3R |

**Rule:**
- **Halve** at about 12σ and **stop** at about 17σ of the setup's own per-trade R (around 9R / 12.5R at σ 0.75). Recompute from the journal once a setup has 50 or more trades.
- **The proposed 5R / 8R is rejected.** Probability of firing within 100 trades (bootstrap of the setup's own R):

  | setup | rule | genuine +0.10R: halved / stopped | zero edge: halved / stopped |
  |---|---|---|---|
  | pump_fade | 5R / 8R | 65% / 24% | 88% / 57% |
  | pump_fade | 9R / 12.5R | 17% / 5% | 47% / 23% |
  | post_pump_decay | 9R / 12.5R | 12% / 2% | 41% / 18% |
  | funding_hi | 9R / 12.5R | 15% / 3% | 47% / 22% |

  At 9R / 12.5R the stop is the 95% test of the claimed edge (Bailey-LdP's stop-out logic). 5R / 8R mostly punishes good setups. The literature agent's simulation (σ 1.55R) agrees: +0.2R setups hit 8R within 100 trades 78% of the time.
- **Limit equity loss through size, not tighter R:** a 12.5R stop at 0.7% risk per trade is about 8.75% of equity. Grossman & Zhou [V] control drawdown by scaling risk with the distance above a high-water-mark floor.
- **Do not use the Bailey & López de Prado closed form (z·σ)²/(4μ) as a live threshold:**
  - It measures loss from one starting point. The exact single-episode quantile, σ²·ln(1/(1−α))/(2μ), is more than twice as large at 95%.
  - A rule watched from every new peak fires far more often.
  - Their [V] triple-penance result still holds: recovery takes about 3× as long as the fall.
- **Time under water:** also review a setup when the trades since its peak exceed (1.645σ/μ)², about 150 trades at σ 0.74.
- **Any fixed stop eventually fires on a good setup.** Expected max drawdown grows like ln T (Magdon-Ismail et al. [V]). So a stop sends a setup to review, not retirement. Retirement on evidence is the registry's CI/decay alarm.
- **Power, honestly:** with σ ≈ 0.75R, telling +0.10R from 0R takes about 225 trades. A drawdown stop is a brake on tail losses, not a skill test.

**Q2, who re-enables.**
- **Evidence:**
  - EU RTS 6 Art. 15(3) [V]: "automatically disabled until re-enabled by a designated staff member".
  - FIA 2024 §3.2 [V]: "disabled until an authorized person re-enables it"; §1.5: the trader must not be able to override.
  - Behavioural: the person responsible for a losing course escalates commitment (Staw 1976), and people avoid realising losses (disposition effect).
- **Rule:**
  - The halve step recovers automatically (DD back under half the halve threshold).
  - A stop sends the setup to shadow / paper only. Re-enable criteria are fixed at stop time, and the trading agent cannot edit them.
  - The re-enable is evaluated by code (governor or registry), never by the agent that proposed the trades. The human is notified.
  - In LIVE mode a human clear is required (RTS 6 / FIA).
  - The setup returns at half size.

**Q3, peak.** All of the theory measures drawdown from the running maximum (Grossman-Zhou; Magdon-Ismail; Bailey-LdP), and 90 days is only 10-30 trades per setup.
- In the agent's simulation, a 20-trade window gave fewer false stops (0.25 vs 0.37) but caught a dead setup more slowly (median 56 vs 40 trades). Windows of 40+ trades behave like the all-time peak.
- **Rule:** use the high-water mark since the setup's current registry version, counted in trades. Reset it only at a journalled review or a rule change (new version).

**Q4, variants.** The Deflated Sharpe paper [V] counts effective independent trials as N̂ = ρ̄ + (1−ρ̄)M, and Harvey & Liu [V] note that correlation "effectively reduces the number of independent tests".
- **Rule:** variants whose daily R correlates ≥ 0.5 with the parent, or whose episodes overlap ≥ 70% (`registry.duplicates`), count in the parent's ladder. This also blocks dodging a stop by relabelling.
- Each variant keeps its own research ledger, and the best variant's Sharpe is deflated by every variant tried.

**Gross vs net and mechanism retirement:** unchanged from the earlier draft. Add `avg_r_gross` next to `avg_r` in `journal.stats.by_setup`, and retire a setup on the day its venue mechanism disappears.

### P2 implementation spec (for binance-bot-15; 2026-10-02)
1. **R per trade:** `journal.closed_results()[i]["r_multiple"]` exactly.
   - It is net P&L (after fees and funding) divided by the initial risk: the pinned `plan.r1_usdt` for adds or split entries, else (initial_stop − entry) × amount.
   - Trades with `r_multiple` None are skipped. Demo and live ledgers are kept apart (`_in_mode`).
2. **Thresholds** (halve / stop, in R):
   - **n < 50 closed trades in the ladder:** use the prior table from the 60-day backtest (bootstrap, μ = +0.10R, 100-trade horizon, q = 0.80 / 0.95):
     - pump_fade 8.6 / 12.3;
     - post_pump_decay 7.7 / 10.8;
     - funding_hi 8.2 / 11.5;
     - bounce_fade 11.1 / 15.7;
     - level_fade 11.3 / 16.2;
     - every other setup 12σ / 17σ with σ = 0.80, i.e. 9.6 / 13.6.
   - **n ≥ 50:** recompute weekly as `shortbot.backtest.drawdown.bootstrap_maxdd(resid, mu=0.10, n=100, paths=4000, block=5, seed=7)` at quantiles 0.80 / 0.95, where `resid` = the ladder's R minus its mean, in closed_at order.
     - Clamp each threshold to [0.75×, 1.5×] its prior, so one noisy week cannot swing it.
     - Reference vector: on the 60-day ledger's selected pump_fade trades this returns 8.57 / 12.34.
3. **Ladder key:**
   - The setup name, plus the epoch = `created_at` of the latest registry version of `setup.<name>`.
   - Only trades opened at or after the epoch count. The high-water mark (HWM) starts at 0 at the epoch.
   - A journalled review may also reset the epoch to "now" (row: who, why, old/new HWM).
4. **State machine** (one kv row per ladder):
   - **States:** NORMAL (1×) → HALF (0.5×) → SHADOW (0×, the setup's scan detections keep being logged as shadow episodes).
   - DD = HWM − cumulative R. Evaluate after every closed trade.
   - **Transitions:**
     - NORMAL → HALF when DD ≥ halve.
     - HALF → NORMAL when DD ≤ halve / 2.
     - NORMAL or HALF → SHADOW when DD ≥ stop.
     - SHADOW → HALF only on re-enable, which then resets HWM to the current cumulative R.
   - **At the stop, store immutable criteria:**
     `{"min_episodes": 30, "min_days": 10, "mean_gt": 0.0, "t_day_ge": 1.0, "size": 0.5, "live_needs_human": true}`
     - They are evaluated by code on the setup's shadow scan episodes since the stop (72h label, day-clustered t). The registry's ≥ 10 distinct days rule (#30) applies.
     - Never evaluated by the agent that proposed the trades, and never editable after the stop.
     - The human is notified at the stop and at re-enable. In live mode re-enable also needs a human clear (RTS 6 Art. 15(3), FIA 2024 §3.2).
   - **Rationale for the re-enable bar:** the stop was a 95% test of "μ ≥ +0.10R". Coming back at half size needs shadow evidence that does not reject μ > 0 at ~84% one-sided, while the burden stays on the setup.
5. **Variant roll-up:**
   - Experiment trades on the same `setup` are already in the parent's ladder (same key).
   - A separately named setup joins a parent's ladder when either:
     - **(a)** the Pearson correlation of daily R sums (UTC close date, zeros on no-trade days) is ≥ 0.5, over ≥ 20 days on which both traded; or
     - **(b)** `registry.duplicates()` reports ≥ 70% episode overlap (same coin within ±6h).
   - Joined setups share one HWM and one state.
6. **Test vectors** (prior pump_fade, halve 8.6 / stop 12.3):
   - **TV1:** R = [+2, −1 × 8] → DD 8.0, NORMAL. One more −1 → DD 9.0 → HALF.
   - **TV2:** continue from TV1 with −1 × 3 → DD 12.0, still HALF. One more −1 → DD 13.0 → SHADOW; the stored criteria equal the dict above.
   - **TV3:** from HALF at DD 9.0, +5 → DD 4.0 ≤ 4.3 → NORMAL. Alternatively +4 → DD 5.0 → stays HALF.
   - **TV4:** trades opened before the epoch are ignored. After an epoch reset, DD = 0 and the state is NORMAL, unless it was SHADOW (SHADOW persists until re-enable).
   - **TV5:** SHADOW with shadow episodes (n 30, days 10, mean +0.4%, t_day 1.2) → re-enable to HALF with HWM = cumulative R, in demo only. In live: pending a human clear. With n 29, or mean −0.1%, or t 0.9 → stays SHADOW.
   - **TV6:** with n = 49 the prior is used; at n = 50 the bootstrap is used, clamped to [6.45, 12.9] / [9.2, 18.5].

## P3. Shadow candidates from the 2026-10-02 studies (no decision needed; for the record)
**Logged, not traded:**
- the long side of `premium_index_reversion` (z ≤ −3);
- `squeeze_fuel_long`;
- the long-trap short (relative edge only);
- the carry/momentum/breakout blend (long leg).

**Sizing gate candidate:** when aggregate BTC/ETH carry is in the top quintile of its trailing year, cut broad alt short size (learning #38). It goes to the human with the other P2 sizing decisions.

# Proposals with their evidence (2026-10-02)

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
- Take-profits are not resting orders. `manager.plan_actions` (manager.py:80-101) decides, and the guardian's 60-second protective pass executes a reduce-only MARKET buy on `markPrice` (guardian_positions.py:199-244, trading_manage.py:32-72). A wick between passes is missed.
- The only orders resting on the exchange are the entry SELL limit, a reduce-only STOP_MARKET pre-arm, one closePosition STOP_MARKET and temporary bridge stops.
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
   - **To measure:** per-target fill prices are not journalled today (`targets_hit` stores target prices only). Record them, and run the demo probe below.

**Still unverified** (both on binance-bot-99's demo-probe list):
- whether Binance cancels reduce-only orders itself when the position goes flat (cancel-on-flat stays mandatory either way);
- the ADL endpoint format.

**Fakes first:** `fake_engine` never fills BUY limits on the way down, and refuses TAKE_PROFIT below price.

### P1b. ADL watch
- Poll the position ADL quantile (`GET /fapi/v1/adlQuantile`, USER_DATA) every 5 min while a position is open. Poll every 60 s while BTC has moved > 3% in 1h or the liquidation stream bursts.
- The response format, weight and quantile range must be **verified against the official docs before building**. Web access was exhausted in this session; the endpoint is named in Binance's ADL FAQ.
- When a short is in the top ADL bucket:
  - alert `adl_risk` with a suggestion to take a partial profit;
  - if `plan.auto.adl` is set, close 25-50% so effective leverage falls.
- Log effective leverage = |notional| / (isolated margin + uPnL) in the guardian marks. It rises as a short wins, which is exactly when ADL ranks it first.

### P1c. Margin asset, mode and buffer checks
- **Account:** alert, or trip REDUCING, if multi-assets mode is on (`GET /fapi/v1/multiAssetsMargin`) or any margin balance is held in a non-USDT/USDC asset (USDe, BNSOL, WBETH and the like).
- **Position:** marginMode must be `isolated`. `_prepare` sets it at entry; re-check it in the resync pass and alert on a mismatch.
- **Margin buffer:** for each isolated short, keep isolated margin ≥ 2× maintenance margin, and keep the liquidation price ≥ `liq_buffer_pct` beyond the stop. On a breach, alert `margin_thin` with "add margin or reduce". This reuses `liquidationPrice` from positions(). There is no new endpoint unless the maintenance margin rate is needed (leverage brackets).

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

## P3. Shadow candidates from the 2026-10-02 studies (no decision needed; for the record)
**Logged, not traded:**
- the long side of `premium_index_reversion` (z ≤ −3);
- `squeeze_fuel_long`;
- the long-trap short (relative edge only);
- the carry/momentum/breakout blend (long leg).

**Sizing gate candidate:** when aggregate BTC/ETH carry is in the top quintile of its trailing year, cut broad alt short size (learning #38). It goes to the human with the other P2 sizing decisions.

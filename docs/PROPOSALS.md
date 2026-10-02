# Proposals awaiting an owner's decision (2026-10-02)

These are designs, not code. Each section says who decides. Evidence lives in [INDUSTRY.md](INDUSTRY.md) §5 and the library cards.

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

### P1a. Deep reduce-only take-profit rungs ("wick harvest")
For every open short, rest 3 GTC reduce-only BUY limits below the deepest plan target:

| rung | price | size (of the position at placement) |
|---|---|---|
| R1 | deepest target or entry, whichever is lower, × (1 − 6%) | 15% |
| R2 | … × (1 − 12%) | 15% |
| R3 | … × (1 − 20%) | 20% |

**How they interact with the rest of the system:**
- **Plan logic:** fills reduce `cur`, and `plan_actions` already counts fractions against the original size, so a rung fill naturally counts toward later targets.
- **Re-placement:**
  - Rungs never sit above a plan target and never exceed the remaining size.
  - After any partial close the guardian cancels and re-places them (one batch call: `POST /fapi/v1/batchOrders`, 5 orders max, weight 5).
- **Kill switch:** rungs are risk-reducing, so they stay in REDUCING and HALTED. A generic cancel-all must skip them, like the protective stop.
- **Flat position:** cancel all rungs when the position reads flat. **Verify on demo** whether Binance cancels reduce-only orders itself when a position closes. The design must not rely on that, because a rung that survives a flat position and then fills would open a LONG.
- **Fakes first** (tests/fakes.py / fake_engine.py, per the CLAUDE.md rule):
  - fill resting BUY limits on the way DOWN;
  - model reduce-only rejection when no position exists;
  - cancel rungs on flat.
- **Measure:** rung fills and the R they add, per trade (`rung_fills` in the trade's entry features).
- **Defaults** (human): sizes 15/15/20% and depths 6/12/20%. They are cheap to change. A deeper or larger last rung raises the payoff in a cascade but gives up more if price bounces before reaching it.

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

## P2. Per-setup drawdown ladder, "pods" (task #21)
**Who decides:** the human, on the thresholds and on whether the stop needs a human clear. It would be implemented with binance-bot-15, who owns risk_budget.py / allocator.py.

**Why:**
- Pod shops kill bad streams early and mechanically. Millennium halves a team's capital at −5% and closes it at −7.5%. Even so it lost ~$900m on crowded index-rebalance bets in Feb-2025.
- Our governor caps the ACCOUNT (daily, weekly and drawdown caps, streaks), but nothing caps a single setup that keeps losing while the others carry it.

**Proposal:**
- **Ledger:** each setup's closed trades in R (net of fees and funding), chronological. Track the running peak of cumulative R and the drawdown from it, with `exp_*` experiments as their own setups.
- **Ladder:**
  - **DD ≥ 5R:** that setup trades at 0.5× size. The guardian or governor applies this, and every proposal shows it.
  - **DD ≥ 8R:** no new entries for that setup until a review. The review is a `research-review` session that writes a learning and clears with a reason (journalled, like kill-switch clears).
  - **Recovery:** full size again once DD ≤ 2.5R (hysteresis), or after a review resets the peak.
- **Gross vs net:** add `avg_r_gross` (before fees, slippage and funding) next to `avg_r` in `journal.stats.by_setup`. If gross is positive and net negative, the problem is costs, not the trigger.
- **Mechanism retirement:** a setup whose venue mechanism disappears is retired on the day, not after a drawdown. Examples: Binance changes the delisting process, or the funding cap.
- **Questions for the human:**
  1. Are 5R / 8R right, given ~1% risk per trade? That is ~5% / 8% of equity lost by one setup.
  2. Should the 8R stop require a human clear, like the kill switch, or can a research-review session clear it?
  3. All-time peak, or a rolling 90-day peak?
  4. Do experiments count toward their parent setup, or stand alone?

## P3. Shadow candidates from the 2026-10-02 studies (no decision needed; for the record)
**Logged, not traded:**
- the long side of `premium_index_reversion` (z ≤ −3);
- `squeeze_fuel_long`;
- the long-trap short (relative edge only);
- the carry/momentum/breakout blend (long leg).

**Sizing gate candidate:** when aggregate BTC/ETH carry is in the top quintile of its trailing year, cut broad alt short size (learning #38). It goes to the human with the other P2 sizing decisions.

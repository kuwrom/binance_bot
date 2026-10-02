# Risk governance (research 2026-09-28)

## Evidence
- **Pressing after wins is a documented bias, not an edge.**
  - Coval & Shumway JF 2005: loss-averse afternoon risk-taking sets prices that reverse fast (costly).
  - Liu-Tsai-Wang-Zhu MS 2010 (Taiwan index options): prior gains lead to more risk, but nothing shows it pays.
  - Thaler & Johnson MS 1990: the house-money effect and the break-even effect.
  - No study found that pressing after wins improves results.
- **Skill persists over years, not intraday streaks** (Barber-Lee-Liu-Odean JFM 2014: fewer than 1% of day traders are reliably profitable). Size by long-run verified expectancy.
- **Naive hot-hand tests are biased** (Miller & Sanjurjo, Econometrica 2018). Use a permutation test.
- **Prop firms:**
  - FTMO 2-step: 5% daily loss, 10% static max. FTMO 1-step: 3% daily loss, 10% trailing max.
  - Topstep: trailing end-of-day maximum loss, about 4% on 50K.
- **Drawdown control** (Grossman-Zhou 1993): risk proportional to wealth above a floor. Stop-losses add value only if returns have momentum (Kaminski & Lo 2014).
- **Kelly:** half-Kelly keeps 75% of the growth rate. P(double before halving) is 0.89 at half-Kelly vs 0.67 at full, and growth is 0 at 2× Kelly (MacLean-Thorp-Ziemba).
- **Conviction ("best ideas")** helps when it's measured and has a track record (Cohen-Polk-Silli), and hurts when untested.
- **Volatility management:** use it to CUT risk only. Moreira & Muir 2017 found gains; Cederburg et al. 2020 found realistic out-of-sample versions usually earn lower Sharpe.

## Rules (defaults; the effective values are in `.env` / research params)
- **Daily cap** = equity × base% × f, with f = min(1, BTC vol_target/vol_now) × regime factor (favorable 1, caution 0.75, hostile or unknown 0.5), floored at 0.5. Hitting it means no new entries until 00:00 UTC. The research base is 1.5%; ours is `daily_loss_limit_pct`.
- **Weekly cap:** 4%, then halt until Monday and review.
- **Drawdown from peak:** ≥ 8% means 0.5× size; ≥ 12% means stop and review. `m_dd` in `risk_budget` also scales continuously.
- **Losing streaks:** 3 consecutive losses means 0.5× until 2 wins or the next UTC day; 5 means a 24h cool-off.
- **Give-back stop:** once the day's peak P&L is ≥ +1%, stop for the day if P&L falls below 50% of that peak. Once the peak is ≥ +2%, lock at least +1%.
- **Conviction:**
  - C: 0.5×.
  - B: 1×.
  - A: 1.5×. A needs ≥ 30 trades of that setup with the lower 90% CI of R above 0; otherwise it's downgraded to B.
  - The combined multiplier (conviction × tilt) is capped at 1.5×.
- **Pyramiding:** only at ≥ +1R (`min_add_r`), each add ≤ 50% of the initial size (`max_add_frac`), at most 2 adds (`max_adds`), and total risk stays ≤ the initial R (`max_add_r_mult`). An add also passes open_short's notional caps, minimum stop distance and liquidation buffer (`risk.check_add`). All are research params: change them with `set_risk_params` under an experiment tag.
- **Moonshot bucket:** 5% of equity, ring-fenced. At most 25% of the bucket at risk per trade. Refilled only quarterly and only from profits. No conviction multipliers.
- **Hot-hand multiplier:** OFF. To enable it:
  - at least 200 closed trades
  - permutation p < 0.01 on lag-1 R, or runs-test z < −2.58
  - E[R | previous win] − E[R | previous loss] ≥ 0.2R, with a bootstrap CI above 0
  - replication on the next 100 trades.

  Even then the cap is 1.25×.
- **Regime gating per setup:**
  - Pre-declare ≤ 4 cells: BTC trend up/down × vol high/low.
  - Need ≥ 30 trades per cell and shrink the cell's expectancy toward the setup mean with weight n/(n+30).
  - Cut a cell to 0.5× only if the upper 80% bound of its shrunk expectancy is below 0. Switch it off only at ≥ 50 trades with a walk-forward check.

## Kill switch (research 2026-10-01; `killswitch.py`)
- **Evidence:** FIA 2024 *Automated Trading Risk Controls*: a kill switch blocks new orders, cancels working orders and may allow risk-reducing ones. MiFID II RTS 6 wants the off switch tested. NautilusTrader's trading states are ACTIVE / REDUCING / HALTED.
- **States** (one journal kv row, `trading_state`, with who, why, when and the cause):
  - REDUCING: only closes, partial closes, stop tightening and cancelling entries. No new entries, adds, risk-raising amends, stop widening or ladder opt-ins. Resting entries stay; a fill is protected, journalled and alerted, never auto-closed.
  - HALTED: REDUCING + every resting limit entry is withdrawn. A pre-armed stop goes only with a limit confirmed unfilled. Protective stops on open positions are never cancelled: for a short book a generic cancel-all strips them.
  - Flatten-all is a command, not a state: HALTED, then every short market-closed reduce-only (`flatten_all` / `shortbot flatten`).
- **Who:** anyone may trip it (`halt_trading`, `shortbot kill-switch HALTED --reason ...`, guardian auto-trips, the session watchdog). Only a human loosens it: `.venv/bin/shortbot-admin clear --reason ...` in a real terminal (it reads a code from /dev/tty and refuses under Claude Code). That is a speed bump plus a tripwire (every change is journalled and alerted), not a security boundary.
- **Auto-trips** (sticky until a human clears, past 00:00 UTC too): daily loss past the effective `daily_loss_limit_pct` and drawdown past `dd_stop_pct` (research override if active, else `.env`), an untracked or long position. A stale guardian heartbeat trips HALTED from any session and lifts itself once the heartbeat is fresh again; withdrawn entries stay withdrawn.
  - Daily loss uses the equity-mark day P&L (`equity_marks.py`): a loss carried over midnight in an open position is not today's loss. A late, degraded or timed-out mark falls back to the old, worse definition. Drawdown is from the transfer-adjusted peak.
- **A clear sticks** (`killswitch_rearm.py`, task #63). Without it the loss trips re-fire on the loss just reviewed.
  - The clear shows and stores a hold: each loss condition at or over its limit, with a guardian reading at most 15 min old, is held at that reading.
  - A held condition trips again only once the loss is `--rearm-margin` points worse (default 1, max 5).
  - The hold lapses at the end of its period (daily loss: the UTC day; drawdown: the ISO week, a weekly review), or once the loss recovers below limit − margin. Then a new loss trips at the plain limit.
  - No fresh reading means nothing is held. Every clear replaces the hold; the decision row records it.
  - After a drawdown clear the governor still halts new entries while drawdown ≥ `dd_stop_pct`. To resume, the human resets the peak: `.venv/bin/shortbot-admin equity-peak reset --reason ...` (live equity becomes the peak; tty code; swaps only the peak shown).
- **Fail closed:** an unreadable state row, or an unreadable state before a stale-heartbeat halt, is REDUCING everywhere, so the auto-clear never restores ACTIVE from it. The guardian keeps the last state it saw in kv, so a loosening while it was down is announced when it starts.

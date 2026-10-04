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
- **Day and week P&L** have one definition, `Trader.account()` (`equity_marks.py`, task #16). Every rule here reads it: the open-short day check, the governor and its give-back peak, and the kill switch's daily-loss trip. Each measures the loss against start-of-day equity.
  - P&L = equity now − the period's first equity mark − transfers since that mark. A loss carried over midnight in an open position is not today's, and deposits and withdrawals move nothing.
  - An equity move the income history doesn't explain yet is `pending` for 2 min, and the income view is used meanwhile.
  - A late or unreadable mark falls back to the old, worse definition: realized since the period start + all unrealized.
  - One income read per `account()` call. If it fails, there is no figure, and nothing is tripped or armed on a guess. Opening paths (`open_short`, limit entries, amends, adds) refuse.
  - In the guardian the pass still succeeds, because its stops were checked: the heartbeat stays fresh, so an income outage never becomes a stale-heartbeat HALTED. It alerts `day_pnl_unverified`.
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

## Resting entries (`entry_gates.py`, #12)
- A resting limit is a trade that hasn't happened yet. While the governor halts new entries, or a market pause is on (BTC/ETH +0.8% in 15 min, a breadth squeeze), the guardian withdraws them every 5 s pass. Reasons read `governor halt: ...` and `market pause: ...`. An entry placed with a `veto_override` over that same pause stays; nothing overrides the governor.
- The governor verdict comes from the guardian's 5-min check, under the effective limits (the research override if active, as for the kill-switch trips), so placing and withdrawing never disagree. A verdict older than 15 min, or a market map older than 15 min, cancels nothing.
- TTL is at most 24 h. Lowering a resting limit re-runs the budget and hard-cap checks. A split leg is never lowered.
- **Resize (#66):** each entry is stamped with the `m_gov` and `m_ladder` it was sized under (a lowering amend re-stamps it). The guardian withdraws a resting entry when:
  - the governor's `m_gov` is now below its stamp (a drawdown tier or a loss streak halved size);
  - its risk (a split entry's legs together) exceeds today's loss-cap room minus open risk, as `compute_budget` caps a new trade;
  - its setup's ladder is SHADOW, or HALF while the stamp is above 0.5.
- A NULL stamp (placed by an older server) skips the multiplier comparison, never the room. An UNKNOWN ladder never withdraws: it already sizes new entries down, and a transient read failure must not churn the book. Only a verdict or ladder read taken after the entry was placed judges it. Reasons read `governor resized: ...` and `setup ladder: ...`; re-propose at the current budget.

## Kill switch (research 2026-10-01; `killswitch.py`)
- **Evidence:** FIA 2024 *Automated Trading Risk Controls*: a kill switch blocks new orders, cancels working orders and may allow risk-reducing ones. MiFID II RTS 6 wants the off switch tested. NautilusTrader's trading states are ACTIVE / REDUCING / HALTED.
- **Tested:** `.venv/bin/shortbot drill kill-switch` (demo, #46; deploy/DEPLOY.md §7a) runs REDUCING, HALTED and flatten through the desk's real command path on a drill short and a far entry, and the owner's clear plus `--verify` records the pass. The go-live gate wants a pass from the last 30 days (docs/GO_LIVE.md).
- **States** (one journal kv row, `trading_state`, with who, why, when and the cause):
  - REDUCING: only closes, partial closes, stop tightening and cancelling entries. No new entries, adds, risk-raising amends, stop widening or ladder opt-ins. Resting entries stay; a fill is protected, journalled and alerted, never auto-closed.
  - HALTED: REDUCING + every resting limit entry is withdrawn. A pre-armed stop goes only with a limit confirmed unfilled. Protective stops on open positions are never cancelled: for a short book a generic cancel-all strips them.
  - Flatten-all is a command, not a state: HALTED, then every short market-closed reduce-only (`flatten_all` / `shortbot flatten`).
  - Resting plan targets (reduce-only BUY limits, #65) only ever reduce risk: REDUCING and HALTED keep them and keep them in sync. Flatten cancels them once flat.
- **Who:** anyone may trip it (`halt_trading`, `shortbot kill-switch HALTED --reason ...`, guardian auto-trips, the session watchdog). Only a human loosens it: `.venv/bin/shortbot-admin clear --reason ...` in a real terminal (it reads a code from /dev/tty and refuses under Claude Code). That is a speed bump plus a tripwire (every change is journalled and alerted), not a security boundary.
- **Auto-trips** (sticky until a human clears, past 00:00 UTC too): daily loss past the effective `daily_loss_limit_pct` and drawdown past `dd_stop_pct` (research override if active, else `.env`), an untracked or long position, and a day P&L unreadable for 3 guardian passes in a row (`day_pnl_unverified`: no new risk while losses can't be checked; resting entries stay; one trip per outage, so a clear made during it holds). A stale guardian heartbeat trips HALTED from any session and lifts itself (next bullet); withdrawn entries stay withdrawn.
  - Daily loss uses the day P&L from `Trader.account()` (Rules above), read once per guardian pass after the stop checks. Drawdown is from the transfer-adjusted peak.
- **Guardian heartbeat** (kv `guardian_heartbeat`, #61) is when protection was last *verified*. Only the guardian's positions pass writes it (the time of its position read), and only when:
  - every held position's check completed with a closePosition stop confirmed resting (or restored, or force-closed), or the account read flat;
  - the 5 s fill poll's last run succeeded (it puts the plan's stop on fills; a send of unknown outcome has no pre-armed stop).

  Not verified: no trade keys, an unreadable position or stop, a failed restore, a symbol a session keeps busy, a long, a plan that raises. `entry_reconcile` (bookkeeping) and the margin pass (alerts) don't count, nor does an income outage (Rules above).
  - Readers act on its age, and every effect lifts itself once a pass verifies: over 180 s (3 passes) new risk is refused, nothing tripped; over 600 s any session trips HALTED and withdraws resting entries, and 2 verified passes lift it.
  - No automatic REDUCING for a long unverified spell: the refusal already stops new risk, a guardian that can't reach the exchange can't act on a state, and its first verified pass afterwards re-runs the sticky loss trips on real numbers. Evidence: 2026-10-02 05:25-08:59 UTC, the laptop slept with dark wakes and no DNS (17 failed passes, desk flat); a sticky trip would have needed a human clear for nothing, and the log shows three such outages in 24 h.
- **A clear sticks** (`killswitch_rearm.py`, task #63). Without it the loss trips re-fire on the loss just reviewed.
  - The clear shows and stores a hold: each loss condition at or over its limit, with a guardian reading at most 15 min old, is held at that reading.
  - A held condition trips again only once the loss is `--rearm-margin` points worse (default 1, max 5).
  - The hold lapses at the end of its period (daily loss: the UTC day; drawdown: the ISO week, a weekly review), or once the loss recovers below limit − margin. Then a new loss trips at the plain limit.
  - No fresh reading means nothing is held. Every clear replaces the hold; the decision row records it.
  - After a drawdown clear the governor still halts new entries while drawdown ≥ `dd_stop_pct`. To resume, the human resets the peak: `.venv/bin/shortbot-admin equity-peak reset --reason ...` (live equity becomes the peak; tty code; swaps only the peak shown).
- **Fail closed:** an unreadable state row, or an unreadable state before a stale-heartbeat halt, is REDUCING everywhere, so the auto-clear never restores ACTIVE from it. The guardian keeps the last state it saw in kv, so a loosening while it was down is announced when it starts.

## Setup ladder (2026-10-02; `setup_ladder.py`, spec in [PROPOSALS.md](PROPOSALS.md) P2, learning #43)
- **What it is:** a per-setup drawdown brake, run like a pod. Its `m_ladder` multiplier sits next to `m_gov` in `risk_budget`.
- **States:**
  - NORMAL is 1×.
  - At `halve`, HALF is 0.5×; it recovers at halve/2.
  - At `stop`, SHADOW is 0×: no new entries, while scans keep logging.
  - UNKNOWN (the ladder can't be replayed) fails closed: 0× if the frozen stop record shows an unreleased stop, else 0.5×. It never withdraws resting entries.
- **Drawdown** is measured in net R from the high-water mark since the setup's current registry version.
- **State** lives in one journal kv row per setup and mode. A stop survives an epoch reset and a new registry version.
- **Thresholds** are the 80% / 95% quantiles of the 100-trade max drawdown of a setup earning +0.10R/trade.
  - Below 50 trades: the prior table.
  - From 50 trades: a bootstrap of the setup's own residual R, clamped to [0.75×, 1.5×] of the prior.
- **Re-enable:**
  - Re-enable criteria are frozen at the stop (30 shadow episodes on ≥ 10 days, mean > 0, day-clustered t ≥ 1) and evaluated by code, never by the proposing agent.
  - A SHADOW setup is re-enabled only by the human, with `.venv/bin/shortbot-admin ladder-clear --setup X --reason ...` in a real terminal, and only once the frozen criteria pass (EU RTS 6 Art. 15(3), FIA 2024 §3.2). `shortbot-admin ladder-status` shows each setup's state and its criteria.
  - The setup returns at HALF.
- **Epoch reset:**
  - A journalled review can start a setup's ledger now: `.venv/bin/shortbot-admin ladder-reset --setup X --reason ...`. Human only (tty code). It records the old state, drawdown and high-water mark.
  - Drawdown and high-water mark restart at 0.
  - A SHADOW stop stays until `ladder-clear`.
  - A new registry version resets the epoch on its own.
- **Roll-up:** variants with daily-R correlation ≥ 0.5 over ≥ 20 common days, or ≥ 70% registry episode overlap, share the ladder.

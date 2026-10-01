---
name: trading-session
description: Run a full short-desk session on Binance futures - review the journal and learnings, check the regime, manage open shorts per their plans, scan 5-10 candidates, and propose trades with plans for human approval.
when_to_use: User says "run a trading session", "let's trade", "check the market", "scan for shorts", "what's happening with my positions".
---

# Trading session

The shortbot state was injected at session start (open positions, alerts since last session, active learnings). If you don't see it, run `.venv/bin/shortbot brief`.

## 1. Learn before acting (2 minutes)
- `start_session`, then `journal`. Read `stats.by_setup` / `by_experiment` (win rate, avg R, MAE/MFE) and the **learnings**.
- Say in one line which learnings apply today.
- Run `session_clock` (`shortbot clock`). It shows the UTC session, minutes to funding, macro events and whether the desk is **cold**.
  - Cold means idle ≥ 6h or the first session of the UTC day. Run `warmup_start`, study the tape for 15 minutes (regime, governor, charts), then run `warmup_check`. Trade only coins that persisted.
  - Warm: skip the warm-up.
  - Time predicts volatility, not direction: widen stops or cut size in soft-warning windows. Macro windows are hard vetoes (docs/SESSIONS.md).

## 2. Regime
- Run `market_regime`.
- **hostile** or **unknown**: manage positions only; no new shorts unless the human explicitly wants a research exception (`veto_override`).
- **caution**: A+ setups only, and consider smaller size.
- Run `market_map` (`shortbot map`) before judging any single coin. It shows the regime, breadth, and each sector's move after removing BTC's move, plus rotation in/out.
  - Short laggards in weak or rotating-out sectors.
  - Never short into a sector rally or a rotation in (hard vetoes, docs/MARKET_MAP.md).
- Run `shortbot governor`. It covers the daily and weekly caps, drawdown tiers, loss streak and give-back stop (docs/RISK_GOVERNANCE.md).
  - A halt means no new entries. Never work around it.
  - Conviction: C 0.5x, B 1x (default).
  - A 1.5x only once the setup has ≥ 30 trades with a proven edge; the code downgrades it otherwise.
  - M is the moonshot bucket: rare, pre-declared, with a written reason.

## 3. Manage open positions FIRST
Load the `manage-position` skill, then for each position:
- `positions` shows the plan, MAE/MFE and the stop on the exchange.
- `coin_snapshot` shows what changed since entry. Compare it with the plan's invalidation and kill conditions.
- Decide: hold / tighten (`move_stop`) / partial (`close_position` with a fraction) / exit / change the plan (`update_plan`). Log the reasoning.

## 4. Scan and shortlist
- `scan_shorts`: every scored coin is logged for research automatically.
- Pick 5-10: the top tradable candidates plus anything interesting from `new_listings_30d` or announcements.
- `coin_snapshot` each one. Load the skill of each candidate's detected setup (`pump-fade`, `listing-fade`, `delisting-short`, `unlock-short`) and apply its checklist.
- **Read the tape** (docs/TAPE.md). Every row has a `tape` block with `state`, `stance` and `playbooks`:
  - `stand_aside`: no entry. This covers pump in progress, liquidation cascade, accepted breakout and spot-led rally.
  - `fade`: failed sweep.
  - `short_bounces`: post-pump decay.
  - `wait_for_trigger`: rest limit entries at levels.
- **Look at the chart** for finalists: `.venv/bin/shortbot chart <coin> --tf 15m`, then Read the PNG. The chart is for review; decisions come from the signals.
- **Don't trust one print** (docs/SOCIAL_AND_ROBUST_PRICES.md):
  - `painted_close`, `mark_gap_z` and `close_engineering` say whether closes are real.
  - Sweeps count only when rejected by time and volume (`sweep_rejected_robust`).
- **Attention:**
  - `mention_hot`/`vol_hot` = stand aside (attention continues).
  - `hype_fade` = the fade setup.
  - `cg_trending_rank` and `binance_hype` are context.
- **Supply shocks (#1 edge, docs/STRATEGY.md):** check `.venv/bin/shortbot unlocks`. Research upcoming team/investor/cliff unlocks of at least 2% of supply and add them with `unlock-add` plus the source URL. The `pre_unlock` tape state and the `unlock` setup pick them up 3–30 days out.

### Read each shortlisted coin
- Run `read_coin` (`shortbot read COIN`). It shows the lifecycle stage, the game on the tape, the controller label and the session, then gives a playbook, a size note, stops and layered evidence (all coins → archetype → this coin).
- "observe" means no known script fits. Log what you see; if it repeats, it becomes a new script.
- Scripts and thresholds change only when the research report (`by_archetype`, `by_lifecycle`, `by_controller`) supports it.
- The `registry` block shows the evidence card for each node the coin sits in (docs/REGISTRY.md).
  - `live` = evidence-backed. `shadow` = hypothesis.
  - Episodes from `forensics` were picked in hindsight: treat them as anecdotes.
  - Say which card supports the trade. If none does, size as C.

## 5. Prefer limit entries at levels (docs/POSITION_MANAGEMENT.md §c)
Unless the trigger needs an immediate market entry, propose a **group of 3-5 limit shorts**:
`.venv/bin/shortbot propose '<json list>'`, or `propose_entries`.
- **Levels:** rest the sell a few ticks below a round number or prior high, or above the stop/liquidation cluster (sweep). Always set `level_type` and `level_reason`.
- **Stop:** beyond the sweep extreme plus a buffer. Set `cancel_below` where the move would have happened without us.
- **Expiry and fills:** GTD expiry on the exchange. The first fill cancels the rest, and the guardian protects the fill within 5 s.
- **Adverse selection:** limits fill more often when you're wrong. Size comes from the current risk budget (`shortbot budget <coin>`).
- **Repricing:** move a pending limit at most once (`amend`). Never chase.
- **No resting bounce-sell limits in breakdowns** (learning #20). They fill on exactly the breakdowns that fail. The breakdown short itself is unproven; if taken, enter on resumption below the breakdown low, not on a hoped-for bounce.

### Writing the invalidation
- Invalidation for a level fade is **acceptance**: 2 consecutive 1h closes beyond the level, or one close beyond the level's measured p90 fake-out (the `sweep_proof` distance).
- Never a single close a fraction of a percent past it. Fake-outs run 2-5% and can close beyond (ZRO #8, 2026-09-29).

## 6. Propose (never execute unilaterally)
Present a compact table per candidate:

| coin | setup | trigger confirmed? | entry | stop (tight or wide) | size/risk | plan (targets, time stop, auto flags) | main risk |

- Only propose entries whose trigger is **confirmed**.
- Everything else goes on the watchlist with `log_decision("watch", ...)`, noting what would confirm it.
- `open_short` always asks the human (it's flagged as requiring approval). Pass the full plan: trigger, invalidation, targets, time_stop_hours, auto flags.

## 7. Close the loop
- `log_decision("session_summary", ...)`: what we did, what we learned, what to check next time.
- If something looked like a pattern: `add_learning` as a **hypothesis**, with the evidence (trade ids, candidate counts).
- Every few sessions, load `research-review`.

## Hard rules
- Never dodge a rejection (splitting orders, sneaking a limit up). Change parameters openly (`set_risk_params`, needs approval).
- Stops only tighten, except ONE `widen-stop` per trade with an automatic size cut (loss stays ≤ 1R). Never average up into a losing short.
- Negative funding or a 1h funding interval (shorts pay) is a **flag, not a veto** (learning #22, human decision 2026-09-29). The code halves the size, and the plan's funding budget exits once funding paid reaches 0.5R with no follow-through. Positive funding is a tailwind, not a trigger.
- Skipping is a valid, logged decision.

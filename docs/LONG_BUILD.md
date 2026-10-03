# Long build plan (task #33, 2026-10-02)
How the short-only desk gets the long trades docs/LONG_PLAYBOOK.md calls for. Covers phases, owners, sequencing against the before-live items, the design rules that follow from the evidence, and a map of every short-only assumption in the code.

## Decision status
- **The owner asked for longs on 2026-10-02:** "and yes i think lets allow the long trading as well", then "but we need a research for longs and the strategies it will follow". The research is done (LONG_PLAYBOOK.md): two setups plus a gate, forward-tested before any size.
- **Still the owner's call:**
  - the sequencing against the gateway (below);
  - every live step. Only the human edits `.env` or switches to live (CLAUDE.md).

## Phases
| phase | what | order code? | owner | when |
|---|---|---|---|---|
| 0 | **Shadow forward test** (`long_shadow.py`): daily job, signals by the backtest's code, shadow trades and labels. No orders | none | built by d5 (`feat/long-shadow`); binance-bot-99 wires it as a daily research job | now |
| 1 | **Side-aware foundation, no behaviour change for shorts:**<br>• a `side` column on trades and entries (default `short`)<br>• one `Side` helper: the closing order side, "tighter of" (min vs max), signed stop distance, liquidation side, PnL and R sign<br>• risk sums on signed worst cases (the four `max(0, …)` sites)<br>• fakes with signed positions | no new paths | d5 (helpers, risk sums, journal) and binance-bot-99 (fakes, reconcile) | beside the gateway work |
| 2 | **Long paths inside the gateway:**<br>• long entries (market and limit groups)<br>• stops below price (closePosition SELL)<br>• close and flatten by SELL<br>• guardian protection for long rows<br>• the plan executor for long plans<br>• side-aware MCP/CLI tools | yes | binance-bot-99 (gateway, guardian, kill switch); d5 (plan rules, risk, skill) | after gateway #36 / typed errors #37 |
| 3 | **Demo longs at C size, mechanical:** a job proposes L1 (Sundays) and L2 (daily) entries at the close with their plans | demo | binance-bot-99 wires; d5 owns the job's rules | after phase 2 passes its tests |
| 4 | **Live** | live | the owner | after the forward test passes (LONG_PLAYBOOK.md) |

## Sequencing: longs after the gateway (recommended)
1. **The long path is mostly order code.** Of the ~150 short-only sites below, about 60 are order, stop, close or flatten paths. That is exactly what the gateway centralises. Building them on today's per-process code and moving them later means writing and testing the riskiest code twice.
2. **Waiting costs no evidence.** Phase 0 starts the forward test now, under the backtest's own fill assumption (daily close). Demo adds execution realism later; it doesn't start the clock.
3. **Phase 1 is safe now.** The side column, the `Side` helper, the signed risk sums and the fakes change nothing for shorts, and the gateway can be written against them from day one.
4. **Revisit if the gateway slips past ~6 weeks.** The forward test should have demo fills by about week 12.

## Design rules (from the evidence, LONG_PLAYBOOK.md)
**Plans:**
- Long plans have a time exit and a catastrophic stop only.
- Targets, breakeven and trailing are off by default, because the edge lives in the tail: the median trade loses about 2%.
- `validate_plan` becomes side-aware.

**Gates:**
- Longs open only while gate G is on (BTC 60-day return > 0).
- The shorts' regime verdict and market pauses stay as they are for shorts.
- No mirrored "long vetoes" are invented. Untested rules don't go in; longs only get the hard vetoes (delisting, hack, exchange status).

**No discretion on L1/L2:** every signal is taken at equal size. The agent's judgement is limited to the hard vetoes.

**Risk:**
- One book. Long and short risk add up with no netting until their correlation is measured. Every heat and pending-risk sum must count a long's risk (today four sites count it as 0).
- L1 sizing is 5 names × 1/5 of a single-trade budget. L2 is equal per signal. Both at C (0.5x) in the forward test.

**Mechanics:**
- **Funding:** a positive rate is the long's cost; the funding size multiplier and flags mirror accordingly.
- **Liquidation:** below entry; the liquidation buffer and `margin.stop_cap` mirror.

**Kill switch and guardian:**
- `flatten` closes longs by SELL; `halt` cancels resting long entries.
- A long *with* a long trade row is protected like a short.
- A position whose side disagrees with its journal row, or that has no row, keeps today's anomaly path (REDUCING, urgent alert).

**Journal and research:**
- The side is stored, never inferred.
- R, MAE/MFE, episodes, reconcile and fill quality all take it.
- Research candidates log their side.

**Fakes first** (CLAUDE.md): fakes need signed positions, stops below price with closePosition SELL, reduce-only SELL limits and the per-side liquidation formula, before any long path is written against them.

**Skill:** a long section in `trading-session`, or a `long-session` skill: L1 weekly, L2 daily, every signal, hard vetoes only, no profit-taking.

## Verification per phase
- **0:**
  - unit tests (in branch);
  - a dry run on live public data (2026-10-02: 65 signals since 2026-09-01, labels and funding correct);
  - the guardian job logs a daily summary.
- **1:** the full suite unchanged for shorts. New tests: a long row's risk is counted in heat, budget and book; the `Side` helper's table; fakes open, stop and close a long.
- **2:** a mirror of every short path test for longs, in the fakes: open, pre-armed stop, fill, stop restore, time exit, catastrophic stop, flatten, halt, side mismatch.
  - Demo probes on the real exchange first: stop below price, closePosition SELL, reduce-only SELL limit.
  - Where live behaviour differs, the fake is fixed first.
- **3:** a dry-run week where the job proposes entries and logs without placing; then C size.

## Short-only assumption map (master 46ba099, read-only survey)
No table has a side column: `trades` (journal.py:52-61) and `entries` (entries.py:52-59) store no direction, and every module works it out by assuming short.

**Silent under-counting:** four places take `max(0, …)` and count a long's risk as 0:
- entry_gates.py:104
- book.py:54
- risk_budget.py:210
- positioning.py:248

### Exchange / trading
- **trading.py**
  - :262 `_place_stop` sends `STOP_MARKET` with side `buy` and closePosition.
  - trading_orders.py:39 `_market_close` is a reduce-only `buy`.
  - trading_orders.py:110 `_market_sell` opens and adds.
  - :160-166 `stop_orders` uses the buy-only `is_protective_stop`.
  - :184 picks the tightest stop with `min`.
  - :189 / :192 PnL and stop-distance signs assume a short.
- **trading_open.py**
  - :66-165 `open_short`: funding mult :105-107; `dist` :110-115; ShortRequest :123; `validate_plan` :131; the short regime verdict :93-94.
  - :205 the through-the-stop test.
  - :235 the risk sign.
  - :237 `stop >= liq*(1-buf)`.
- **stops.py**
  - :40-46 `is_protective_stop` requires `buy`.
  - :96 the bridge stop is a `buy`.
  - :120 / :154 / :157 "tighter" = `min`.
  - :274-278 a position-flip alarm fires unless the side is short.
  - :216-234 the alert texts.
- **trading_manage.py**
  - :209 `current = min`.
  - :211 the only-tighten rule and :214 the mark rule (`new_stop >= current`, `<= mark`) flip.
  - :217 the distance sign.
  - :254 the fallback stop `entry*(1+%)`.
  - :256 / :263 the cap and close-if-past rules.
  - :276-284 `_liq_cap` with ROUND_FLOOR.
  - :307-313 `cap_stop`.
  - :179 `_cover`.
- **risk.py**
  - :61-62 stop distance sign.
  - :81-88 `liquidation_price(-1, …)` is hardcoded; `max_leverage_for_stop` and `max_notional_for_risk` (:91-104) depend on it.
  - :121-123 `funding_mult` treats negative funding as the cost.
  - :131-132 / :158 "stop above" and "already short" messages.
  - :197-213 `check_add`.
- **margin.py**
  - :56-61 `parse_adl` falls back to the SHORT quantile.
  - :72-74 `stop_cap = liq*(1-buf)`.

### Entries desk
- **entries_place.py**
  - :119-120 the limit must be above the market.
  - :131 the stop must be above the limit.
  - :140 the distance sign.
  - :172 the short liquidation estimate.
  - :196 ShortRequest.
  - :229 the entry is a GTD `limit sell`.
  - :270 the pre-armed stop is a `STOP_MARKET buy`.
  - :66 the far leg is the highest price.
  - :244 the default target `px*(1-2*dist)`.
  - :255 the risk sign.
  - :148 / :151 funding and liquidity checks.
- **entries.py**
  - :231 `cancel_below` (a long needs `cancel_above`).
  - :233 the fast-approach test only checks a rising price.
  - :95 reads `verdict_for_shorts`.
- **entries_amend.py**
  - :32 `new_limit >= stop`.
  - :36 the direction that raises risk.
  - :43-44 the risk sign.
  - :59 edits as `sell`.
- **entries_fills.py**
  - :94-97 `plan_stop` uses max / min for "looser".
  - :203 / :207 claims fills from sells; the MAE sign.
- entry_intents.py:76 the risk sign.

### Guardian
- **guardian_positions.py**
  - :162-168 any non-short position: `unexpected_long`, REDUCING, and its stop is not protected.
  - :177 `stop_near`.
  - :26 the fallback stop at +10%.
- guardian_plan.py:51 `min`.
- **manager.py**
  - :34-40 excursions (`low_since_entry`).
  - :66-67 the funding-budget R.
  - :90-92 the target order and reached test.
  - :112-113 breakeven.
  - :43-51 `_clear_round`.
  - :117-118 trailing.
  - :122 the stop-vs-mark guard.
- **plans.py** :78-79 / :89-90 "target below entry" and "stop above entry".
- **targets.py** :62 / :66 the target order; :131 the slippage sign.
- **targets_sync.py** :160 target limits are reduce-only `buy`.
- **guardian_watch.py**
  - :42 watches shorts only.
  - :92-97 squeeze alerts watch for up-moves.
  - :98-100 "shorts now PAY".
- **guardian_margin.py**
  - :98 shorts only.
  - :107 the cap test.
  - :68-71 the ADL text.
- **positioning.py**
  - :26-32 R.
  - :44-49 the chandelier stop.
  - :60-92 `reassess`.
  - :119 / :127 the short verdict and short bracket.
  - :153-163 `widen_stop`.
  - :226-236 `add_to_winner`.
  - :248 risk.
  - :261 adds by `_market_sell`.

### Kill switch
- killswitch_ops.py:59-63 `flatten` skips non-shorts, and closes through `Trader.close`, which buys.
- killswitch_ops.py:85 and mcp_kill.py:37: texts.
- `halt` / `cancel_resting` and the state logic are already side-agnostic.

### Risk / governor / budget / ladder
- **risk_budget.py**
  - :208-210 open risk counts a long as 0.
  - :81-82 / :114 correlation and cluster heat add all positions (no netting: right for gross risk).
- **book.py** :54 pending risk; :118-129 "alt shorts".
- **governor.py** :25 `REGIME_MULT` is keyed on the short verdict; the rest is side-agnostic.
- **ladder.py**
  - :39-46 `race_with_edge`.
  - :49-72 `state_score`.
  - :75-104 levels and `evaluate_ladder`.
  - :154-172 placebo and adaptive stop.
  - :193 / :227 R and the stop test.
- **ladder_apply.py** :28-41 / :84.
- **setup_ladder.py** :267-271 shadow evidence is `short_ret_72h`.

### Journal / reconcile
- **journal.py**
  - :52-61 no side column.
  - :58 / :215-216 `low_since_entry`.
  - :269 the R sign.
  - :99-107 `exit_quality` uses `further_drop_24h_pct`.
- **episodes.py**
  - :53-74 `span` (sells add, buys reduce).
  - :85 / :91-107 / :150 episodes start at a sell.
- **reconcile.py**
  - :75 exit fills are buys.
  - :190-199 `_opening` doesn't check the side.
- fill_report.py:63-68 and fill_quality.py:145-150 assume sells and buys.
- targets.py:160-165 reads buys.
- research.py:87-110 post-exit labels.

### Signals / tape / regime / scanner / probability
- **The short verdict:**
  - market/regime.py:28-39 `verdict_for_shorts`.
  - tape.py:13-61 stances.
  - signals/base.py:7 vetoes mean "don't short".
  - signals/core.py:59-62 funding sizing.
  - signals/tape.py:41-173 vetoes.
  - market/market_map.py:20-21 and :148-165 pauses.
- **scanner.py**
  - :36 `scan_shorts`.
  - :83-87 `entry_checks`.
  - :103-131 suggested stops are above price.
  - :156-173 short brackets.
- sweeps.py:229-244 only looks at levels above price.
- read.py:49 / :119, allocator.py:24-59 and setups.py are short theses.
- **probability:**
  - engine.py:32-82 `vol_scaled_bracket` and `short_bracket`.
  - baseline.py:75-85 `short_geometry`.
  - montecarlo.py:42 `simulate_short`.
  - labels.py:10-23 `label_bracket`.
- journal_research.py:20 the `short_ret_*` columns.

### MCP / CLI
- **mcp_trade.py**
  - :82-99 `open_short` has no side.
  - :105 / :115 texts ("move DOWN only").
  - :131 `validate_plan`.
  - :156 the short verdict.
  - :168 / :175 `add_to_winner`, "ABOVE market" and `cancel_below`.
- mcp_desk.py:19 and mcp_research.py:30-37 `short_bracket`.
- mcp_server.py:29-49 instructions.
- cli_args.py:28 / :40 / :160 and cli.py:147-204.

### Fakes (no long support today)
- **tests/fakes.py**
  - :201 `assert side == "buy"` for stops.
  - :209 the stop direction.
  - :152-155 `_algo_view` returns `buy`.
  - :223 reduce-only must be a buy.
  - :240-266 the buy-limit routing and fault hooks are side-bound.
  - :357 `trigger_stop` buys.
- **tests/fake_engine.py**
  - :61-80 an up-walk fills sell limits and buy stops only.
  - :93 records a buy.
  - :126-151 unsigned qty.
  - :193-199 the short liquidation formula.
- **tests/fake_account.py** :19-38 `positionAmt=-qty`, side short; :114 / :123 force-close buys.
- **tests/fake_reduce.py** :26-77 buy limits only.
- **Tests that pin "a long is an anomaly"** (they become "a long without a long row"):
  - test_killswitch_guardian.py:143
  - test_killswitch_heartbeat.py:114
  - test_stop_swap.py:221
  - test_guardian_loop.py:303

### Already side-generic (reuse)
- backtest/sim.py `Plan.side` (±1, funding sign) and backtest/placebo.py mirroring.
- risk.py:73-78 `liquidation_price(side, …)`.
- probability/baseline.py touch, race and three_way helpers; labels.py `label_touch(above=)`.
- tape_math.py round zones (above and below).
- Already side-agnostic:
  - close_intent.py, entries_cancel.py and tranches.py;
  - clientid.py, symlock.py and the stops.py make-before-break structure;
  - the targets_sync.py listing and settle machinery;
  - equity marks, the governor (except `REGIME_MULT`), kill-switch state and halt;
  - reconcile's income, finalize and exit reason; margin's position findings and effective leverage;
  - scan_triggers and the liquidation recorder (both sides).

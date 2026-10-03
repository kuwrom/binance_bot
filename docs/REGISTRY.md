# Pattern registry

This is how the library of tapes, scripts and strategies stays honest as it grows. The design comes from learning #14.

**Code:**
- `registry.py`: tables, evidence cards, status rules, decay alarms, lesson search.
- `registry_seed.py`: seeds the registry and feeds forward episodes (scans, closed trades) into it.
- `registry_forward.py`: frozen plans, the forward test, decay (#26).
- CLI: `shortbot registry tree | card ID | search TEXT | seed --bt-dir DIR | status ID --status S --reason R | forward [ID]`.

## Records
| table | holds |
|---|---|
| `patterns` | One row per (id, version). Fields: level (regime, lifecycle, tape, controller, script, setup, detector, strategy, filter, veto), parent, mechanism (who is forced to trade), definition, detector, metric, status, source. |
| `pattern_episodes` | One row per occurrence: symbol, time, source (scan / bt60 / week_study / forensics / live), `oos` flag, outcome (in the metric's unit), win, MAE, as-of features. |
| `pattern_plans` | One frozen plan per (id, version): registration time, spec hash, metric, target sample, decision rule. The table refuses UPDATE and DELETE. |
| `pattern_trials` | Legacy (#31): every parameter set tried up to #31. Now every study writes to the trial register (`data/research/trials.db`, docs/BACKTEST.md "Trial register"); this table was copied in once after #31. Cards count a pattern's trials from the register (the legacy count when the register is absent). Exploratory: the backtest's deflated Sharpe counts them; the forward family (M) does not. |
| `pattern_learnings` | Links from patterns to `learnings`. |
| `lessons_fts` | FTS5 index over pattern definitions (rebuilt by `registry seed`). Learnings have their own index, `learnings_fts`, kept current by triggers (`journal_learnings.py`); `read_coin` and `registry search` query both. |

## Rules (enforced in code)
- **A new version** is created when the definition, detector, metric or an explicit plan changes. Old episodes stay with the old version.
- **Status flow:** draft → shadow → **live** → probation → retired.
- **Promotion to live is refused** unless the version's forward test passed (below). Backtest "OOS" episodes, week-study and forensics rows are shown on the card, but they never promote.
- **Decay alarm:** the guardian checks after each labelling pass, on the last 90 days of forward rows, clustered by UTC day (CV3, t(days-1)).
  - live → *probation* when that 95% CI crosses 0 (≥ 10 days);
  - probation → *retire* when its mean is below 0 (≥ 20 days).
  - A live node without adjusted rows is judged on its raw live rows, still day-clustered.
- **Retired rows stay** in the table: they still count in M.
- **Duplicate gate:** `duplicates(pid)` flags any active pattern whose episodes coincide (same coin within ±6h) with 70% or more of this one's.
- **Hindsight episodes:** `forensics` episodes were picked in hindsight. The cards show the mix of sources, so anecdotes are never mistaken for evidence.

## Forward test (#26)
Exploration stays free: anything can be mined, registered and traded as a hypothesis. Only promotion to live needs a prediction made before the data existed (Nosek et al. 2018).
- **Plan, frozen at registration** (`pattern_plans`): metric, target sample, decision rule and a hash of the whole spec. Any edit is a new version: a new hypothesis with a new date. Versions older than #26 got the default plan when the first feed or seed ran after the deploy; their clock starts there.
- **Data:** only `scan` and `live` episodes newer than the plan. The weekly `hist.db` rebuild (bt60, week study) never enters. Each feed rebuilds a node's forward rows, so relabels and late labels propagate.
  - *Scan nodes* (lifecycle, tape, controller, setup, `detector.pump_fade_discount_scan`): one row per symbol-episode (rescans within 24h are one). `adj` = 24h short return − 0.1% round trip + funding carry − the median of the same scan's unflagged coins (≥ 5). Also shown: `vs_market` (all coins), `vs_control`. Default target: 100 episodes over ≥ 20 days.
  - *Strategy nodes with a setup* (`read.SETUP_STRATEGY`: pump_fade, bounce_fade, listing_fade, delisting): closed trades, demo and live. `adj` = net R (fees and funding included) − the control's 24h median × hold/24h, in R. Default target: 30 trades over ≥ 20 days.
  - Everything else (vetoes, filters, scripts, week-study detectors, strategy variants) has no forward feed: not promotable until re-registered with one.
- **Decision, once:** on the first `target_n` observations that span `min_days` days. That stopping point never depends on outcomes, so looking early costs nothing (Miller, "How not to run an A/B test"). One-sided day-clustered t; pass iff p ≤ α / M, α = 0.025, M = forward-decidable non-draft versions registered so far (Bonferroni; Harvey, Liu & Zhu 2016). Until then the report shows the interim mean without an interval. A failed version stays failed: test again by registering a new version, which grows M.
- **Where:** `card` → `forward` and `forward_90d`; `read_coin` (compact); `shortbot registry forward [ID]`.

## Where it shows up
- `shortbot read COIN` → `registry`: this is the coarse cut. It lists the nodes the coin sits in right now (lifecycle stage, tape state, controller labels, setups, and the matching backtest strategy), each with an evidence card (all / OOS / last 90 days, trials, verdict), plus the matching lessons.
- **Forward episodes** reach the tree automatically. Each time the guardian labels scans, labelled candidates (one per symbol-episode) and closed trades newer than a node's plan are rebuilt into its episodes.

## Seeded 2026-09-29
**Patterns registered:**
- 8 lifecycle stages
- 5 controller labels
- 12 tape states
- 5 setups
- 8 week-study detectors (1,058 episodes)
- 15 forensics scripts (72 episode labels, hindsight)
- 12 backtest strategies (36 trials, 25k trades of the selected variants)

**Status:** everything is `shadow`; nothing qualifies for live yet (learning #17). Since #26 only the forward test can change that.

## Staging (learning #14)
- **Now:** registry, tree, cards, trials, status flow and lesson search.
- **100+ labelled episodes per family:** a panel of k-nearest analog episodes plus meta-labelling (tasks #75/#76).
- **1,000+ episodes:** clustering and learned embeddings, adopted only if they beat the hand-built features walk-forward (task #77).

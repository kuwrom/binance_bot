# Pattern registry

This is how the library of tapes, scripts and strategies stays honest as it grows. The design comes from learning #14.

**Code:**
- `registry.py`: tables, evidence cards, status rules, decay alarms, lesson search.
- `registry_seed.py`: seeds the registry and feeds live episodes into it.
- CLI: `shortbot registry tree | card ID | search TEXT | seed --bt-dir DIR | status ID --status S --reason R`.

## Records
| table | holds |
|---|---|
| `patterns` | One row per (id, version). Fields: level (regime, lifecycle, tape, controller, script, setup, detector, strategy, filter, veto), parent, mechanism (who is forced to trade), definition, detector, metric, status, source. |
| `pattern_episodes` | One row per occurrence: symbol, time, source (scan / bt60 / week_study / forensics / live), `oos` flag, outcome (in the metric's unit), win, MAE, as-of features. |
| `pattern_trials` | Every parameter set ever tried, with n, coins, mean, t, and OOS n and mean. These feed the multiple-testing count. |
| `pattern_learnings` | Links from patterns to `learnings`. |
| `lessons_fts` | FTS5 index over learnings and pattern definitions. |

## Rules (enforced in code)
- **A new version** is created when the definition, detector or metric changes. Old episodes stay with the old version.
- **Status flow:** draft → shadow → **live** → probation → retired.
- **Promotion to live is refused** unless the out-of-sample evidence clears all three:
  - at least 30 distinct coins;
  - a 95% CI above 0 (coin-clustered);
  - t ≥ 3, both coin-clustered and day-clustered. If there is no day-clustered t (every OOS episode on one day), the pattern is not eligible, and the verdict says why.
- **Decay alarm:** the guardian checks after each labelling pass.
  - live → *probation* when the last-90-day CI crosses 0 (n_coins ≥ 10);
  - probation → *retire* when the last-90-day mean is below 0 (n_coins ≥ 30).
- **Retired rows stay** in the table, so they still count as trials.
- **Duplicate gate:** `duplicates(pid)` flags any active pattern whose episodes coincide (same coin within ±6h) with 70% or more of this one's.
- **Hindsight episodes:** `forensics` episodes were picked in hindsight. The cards show the mix of sources, so anecdotes are never mistaken for evidence.

## Where it shows up
- `shortbot read COIN` → `registry`: this is the coarse cut. It lists the nodes the coin sits in right now (lifecycle stage, tape state, controller labels, setups, and the matching backtest strategy), each with an evidence card (all / OOS / last 90 days, trials, verdict), plus the matching lessons.
- **Live episodes** reach the tree automatically. Labelled scan candidates (72h outcome) become episodes of their nodes each time the guardian labels them.

## Seeded 2026-09-29
**Patterns registered:**
- 8 lifecycle stages
- 5 controller labels
- 12 tape states
- 5 setups
- 8 week-study detectors (1,058 episodes)
- 15 forensics scripts (72 episode labels, hindsight)
- 12 backtest strategies (36 trials, 25k trades of the selected variants)

**Status:** everything is `shadow`; nothing qualifies for live yet (learning #17).

## Staging (learning #14)
- **Now:** registry, tree, cards, trials, status flow and lesson search.
- **100+ labelled episodes per family:** a panel of k-nearest analog episodes plus meta-labelling (tasks #75/#76).
- **1,000+ episodes:** clustering and learned embeddings, adopted only if they beat the hand-built features walk-forward (task #77).

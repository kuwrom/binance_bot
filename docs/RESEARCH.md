# Growing the desk: research loop and extension guide

## The loop
```
scan_shorts ──► candidates (features at decision time) ──72h──► outcomes (guardian labels)
     │                                                              │
     ▼                                                              ▼
open_short ──► trade (plan, entry features, experiment) ──► exchange-truth PnL, R, MAE/MFE
     │                                                              │
     └──────────────► research_report / journal stats / lab ◄───────┘
                                   │
                                   ▼
                  learnings (hypothesis → supported → adopted | rejected)
                                   │
                                   ▼
            change: score weights, vetoes, setups, plans, risk params (as named experiments)
```
Scans are the fast data source: 15–25 coins per scan, labelled automatically, whether or not we trade. Trades are the slow, true source: real fills, fees, funding and slippage.

## Where each kind of change goes
| Change | How | Measured by |
|---|---|---|
| Risk limit (leverage, size, stop range...) | `set_risk_params` with an experiment tag | `journal.stats.by_experiment` |
| One-off exception to a veto or regime | `open_short(veto_override=...)` (or a proposal's `veto_override`) | `entry_features.veto_override` + `vetoes_overridden` on the trade |
| One-off size above the risk budget | `open_short(notional_usdt=..., budget_override=...)` | `entry_features.budget_override` + `budget_overridden` (risk at stop vs budget) |
| New signal or indicator | One `@signal` function in `signals/` (new data feed → getter in `market/hub.py`). It's logged automatically. | `research_report.by_feature_tertile` |
| New tape state / playbook routing | One `TapeRule` in `tape.py` | tape state vs candidate outcomes |
| Signal half-life | `decay.HALF_LIFE_H` prior; refit with `decay.calibrate_half_life` (IC vs signal age) | IC(age) fit, shrunk to the prior |
| New social/attention source | Fetcher in `market/social.py` + snapshot in the guardian job (never inline in scans) | `research_report.by_feature_tertile` |
| Unlock / scheduled event | `shortbot unlock-add` with a source; the `unlock` signal and setup pick it up | `calibration` by setup=unlock |
| Score weights / vetoes | Edit the signal's points/vetoes in `signals/*.py`, with tests | `research_report.by_score`, `by_veto_reason` |
| New strategy idea (any direction) | A card in `docs/strategies/*.md`; `shortbot registry seed` registers it as a `lib.<id>` draft | moves to shadow once it has a detector and episodes |
| New setup | Trade as `exp_<name>`, then register it in `setups.py` and add `.claude/skills/<name>/SKILL.md` | `journal.stats.by_setup`, `research_report.by_setup` |
| Management rule for one trade | `update_plan` | MAE/MFE and R on that trade |
| Default management of a setup | `Setup.default_plan` in `setups.py` | `by_setup` avg R, MAE/MFE |
| Backtest slippage model | `fill_report.calibrated_costs(journal)` once `calibrate()` is ok (30 trades, 20 days, one mode) | `journal` → `stats.fill_quality`: demo fills vs the mainnet price at each fill, in bps, vs the model |
| Guardian alert thresholds | Constants at the top of `guardian_positions.py` (job cadence and timeouts: `guardian.py`) | alert usefulness (journal alerts) |
| New exchange caller (job, tool, script) | Tag its entry point with a weight class (`weight.tagged`): order paths protective, scans research, untagged = desk. A new guardian lane goes in `weight.GUARDIAN_LANES` (docs/SCANNING.md, weight guard) | `budget_wait` errors, 429/418 warnings in the logs |
| New data source (paid API later, etc.) | New module, then add it to `build_metrics`/`news` behind `data_missing` so failures fail closed. Any third-party text it returns reaches the agent only as `untrusted.wrap(...)`, never in the brief | as above |

## Rules of evidence
- n < 30 is an anecdote. Say so when recording a learning.
- Rescans are not new evidence: count symbol-episodes (`research_report` `n`, since 2026-10-01) and days, not rows. With fewer than 20 days, intervals are descriptive only (`research_stats.py`).
- Compare against the baseline (`research_report.all`), not against zero: in a falling market everything "works".
- Promotion to live is decided only by the registry's pre-registered forward test (docs/REGISTRY.md "Forward test"): data newer than the frozen plan, vs the same scan's control after costs, at α / M. Everything else is exploration.
- Watch `max_adverse_24h` and `squeeze_rate_pct`, not just average return. A short edge that sometimes costs +50% isn't an edge at 3× leverage.
- Keep one experiment at a time per parameter, so results are attributable.
- Methods we don't have yet (PBO/CSCV, SPA, meta-labeling, transition matrices and HMM regimes, HAR/GARCH σ, vol targeting) and where to learn them: docs/QUANT_METHODS.md.
- Every study records each trial it runs in the trial register (`data/research/trials.db`, docs/BACKTEST.md "Trial register"); the Deflated Sharpe's N is taken over all of them. Every reported t is the t of the reported mean, clustered by day, coin and both (`cluster_se.py`, #31). Older write-ups say "pre-#31 estimator".
- Cite a backtest or study by its stamped dir, `data/research/<UTC date>-<sha7>/...` (code, data fingerprint, window and config in `provenance.json`; docs/BACKTEST.md "Provenance"). Scratchpad and `/tmp` outputs vanish.
- Every learning has a `kind`: `engineering` (a fix, backed by a test), `statistical` (a claim about outcomes) or `process` (how the desk works). Statistical ones carry `n` (independent observations: episodes, trades or events, never rows), `days` and a `sample` link (`research_report:<date>`, `registry:<id>` or `data/research/<dir>`). `review_by` defaults to the last update + 14 days for a hypothesis and + 30 for a supported statistical learning. The brief and `research_report.learnings` flag what to re-check: review due, supported/adopted on n < 30, and statistics recorded before 2026-10-01 (row counts). Rows recorded before these fields show inferred values marked `?` (`learnings_view.py`); set them with `update_learning`. These are flags, never gates.
- **The rulebook and search** (`learnings_brief.py`, `journal_learnings.py`): the brief always loads at most 20 *settled* learnings, each as its one-line `rule` (≤ 160 chars; without one, the statement cut short and the `missing_rule` flag). Settled = not superseded, not expired, not `engineering`, and either `adopted` or `supported` without the `anecdote`, `n_unknown` or `harmful` flag. Below it, "relevant now" adds at most 5 current learnings found by full-text search (FTS5, stemmed; kept current by triggers) from the ids open trades cite, the open trades, the resting entries and the last 6 h of scans. Everything else is searched on demand: the `learnings` tool (query, setup, ids). The `journal` tool returns a compact index.
- **Change a learning item by item, never by a rewrite.** Evidence and provenance are appended. The statement never changes: a changed claim is a new learning, and the old one gets `superseded_by` (it leaves the brief and searches, and keeps its history). Give a regime- or one-coin claim a `valid_until` date; past it, the learning is `expired` until someone extends, supersedes or rejects it. `add_learning` returns the `similar` learnings it found first: update or supersede one of them rather than keeping two.
- **Outcome scores, never votes.** A plan cites the learnings its entry relies on in `uses_learnings` (set at entry only, unknown ids refused). Each closed trade (any mode) scores each learning it cited: helpful when R > +0.05, harmful when R < −0.05, neutral in between. Scores are computed when read, from `closed_results`. `harmful` (harmful ≥ helpful + 2) keeps a supported learning out of the rulebook and is listed for re-checking; an adopted one stays until someone decides.

## Risk parameters as experiments
- **Layers:** baseline limits come from `.env`. The AI may override any of them via `set_risk_params(changes, reasoning, experiment)`, with no approval step, but only within `BOUND_*` (human-only, `.env`). `reset_risk_params` drops the overrides.
- **Scope:** overrides apply in demo only, unless `AI_PARAMS_IN_LIVE=true`, and are stored per mode: a demo experiment never carries into live. The guardian always uses the baseline.
- **Tracking:** each override belongs to the experiment that last set it. A trade carries every experiment still active (sorted, joined by ` + `, e.g. `lev10_risk3 + liq_tiers`) and the full limits it was sized under (`entry_features.limits`; limit fills get them at placement). Compare with `journal` → `stats.by_experiment`; `param_sets` > 1 means one label covers different limits. Promote a winning experiment by writing it into `.env` (human).
- **Bounds win:** overrides are re-checked against the current `BOUND_*` on every load. One the human's bounds no longer allow is dropped back to the `.env` value (not clamped), with a `params_dropped` alert and decision.
- **Demo vs live:** trades and entries carry `mode`. Stats, the governor's streak, tilts and open trades read the current mode only (`all_modes=True` for both); the equity peak, period marks and day peak are kept per mode.
- **Liquidation:** the code always refuses a stop beyond the estimated liquidation price. Max leverage ≈ 100 / (stop% + liq_buffer_pct + 1).

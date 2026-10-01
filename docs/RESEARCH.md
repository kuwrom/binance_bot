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
| One-off exception to a veto or regime | `open_short(veto_override=...)` | `entry_features.vetoes_overridden` on the trade |
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
| Guardian alert thresholds | Constants at the top of `guardian_positions.py` (job cadence and timeouts: `guardian.py`) | alert usefulness (journal alerts) |
| New data source (paid API later, etc.) | New module, then add it to `build_metrics`/`news` behind `data_missing` so failures fail closed | as above |

## Rules of evidence
- n < 30 is an anecdote. Say so when recording a learning.
- Compare against the baseline (`research_report.all`), not against zero: in a falling market everything "works".
- Watch `max_adverse_24h` and `squeeze_rate_pct`, not just average return. A short edge that sometimes costs +50% isn't an edge at 3× leverage.
- Keep one experiment at a time per parameter, so results are attributable.

## Risk parameters as experiments
- **Layers:** baseline limits come from `.env`. The AI may override any of them via `set_risk_params(changes, reasoning, experiment)`, which needs human approval, and only within `BOUND_*` (human-only, `.env`). `reset_risk_params` drops the overrides.
- **Scope:** overrides apply in demo only, unless `AI_PARAMS_IN_LIVE=true`. The guardian always uses the baseline.
- **Tracking:** trades opened under an experiment carry its tag, so compare with `journal` → `stats.by_experiment`. Promote a winning experiment by writing it into `.env` (human).
- **Liquidation:** the code always refuses a stop beyond the estimated liquidation price. Max leverage ≈ 100 / (stop% + liq_buffer_pct + 1).

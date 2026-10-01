---
name: research-review
description: Review what the desk has learned - research_report on logged scan outcomes, trade stats by setup/experiment, ad-hoc analysis with shortbot.lab, and updating learnings, vetoes, setups and parameters.
when_to_use: Every few sessions, when the user asks "what have we learned", "is this working", "should we change X", or wants to test a new idea / setup / indicator.
---

# Research review

The desk learns from three data streams:
1. **Scan candidates**: every coin `scan_shorts` scored, with all features, labelled 72h later with what a short would have done (`short_ret_4h/24h/72h`, `max_adverse_24h`). This is the largest and fastest dataset, and it grows without trading.
2. **Trades**: exchange-truth PnL (fees and funding included), R multiple, MAE/MFE, the setup, the experiment and the entry features.
3. **Learnings**: what we concluded, with evidence and status (hypothesis → supported → adopted, or rejected).

## Standard review
1. `research_report`:
   - Does a higher score actually mean a higher `avg_short_ret_24h`?
   - Which vetoes save us (the vetoed coins squeeze) and which cost us (the vetoed coins dropped anyway)?
   - Which feature tertiles separate winners? These are exploratory (`feature_mining`): confirm on later data.
   - Since 2026-10-01 (`basis`), `n` counts symbol-episodes (rescans of a coin within 24h are one bet), `rows` the raw scan rows and `days` the independent market days. Compare groups by `avg_excess_ret_24h` (vs the median of the same scan) and its `ci95_excess_ret_24h`; `descriptive_only` means fewer than 20 days. Learnings that cite row-based n from before 2026-10-01 overstate their evidence.
2. `journal`: stats by setup and by experiment. Treat n < 30 as anecdote.
3. Update learnings (`add_learning` / `update_learning`), citing the numbers.

## Ad-hoc questions: code mode
Use `shortbot.lab` through Bash and print only the conclusion:
```bash
.venv/bin/python - <<'PY'
from shortbot import lab
rows = lab.candidates(labelled=True)
print(lab.group_mean(rows, lambda r: (r["features"]["funding_8h"] or 0) > 0.001, "short_ret_24h"))
print(lab.group_mean(rows, lambda r: r["features"]["lower_high"], "max_adverse_24h"))
PY
```
Also available: `lab.ohlcv`, `lab.oi_history`, `lab.funding_history`, `lab.taker_ratio`, `lab.tickers`, `lab.snapshot`, `lab.metrics` (guardian history), `lab.trades`, `lab.report`, plus the indicators `lab.rsi/ema/atr/vwap_bands`. The module is read-only: no orders.

## Acting on findings
Propose changes to the human first. Code changes need tests (`.venv/bin/pytest -q`).
- **A threshold looks wrong**: open a named experiment with `set_risk_params` (risk limits), or propose a code change to the signal in `signals/*.py` (score points and vetoes).
- **A new signal**: write one `@signal(...)` function in `src/shortbot/signals/` (data via the MarketContext, which adds a getter in `market/hub.py` if a new feed is needed). Its features are logged and reported automatically. Give it points or vetoes only when the data supports it (`research_report.by_feature_tertile`).
- **A new tape state**: add one `TapeRule` in `src/shortbot/tape.py`.
- **A new setup**: trade it as `exp_<name>` first. Once the evidence holds, register it in `setups.py` with a detector and default plan, and add a skill in `.claude/skills/<name>/SKILL.md`.
- **A veto costs more than it saves**: `add_learning` with the numbers, then propose relaxing it. Single trades can bypass it with `veto_override`.
- **Retire** a setup with `status="retired"`. History is kept.

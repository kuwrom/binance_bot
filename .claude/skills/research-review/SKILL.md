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
   - `forward`: each registry node's pre-registered forward test. It is the only evidence that promotes a node (docs/REGISTRY.md); everything else above is exploration.
2. `journal`: stats by setup and by experiment. Treat n < 30 as anecdote. `stats.fill_quality` compares demo fills with the mainnet price at each fill (bps) and with the backtest's slippage model: check it before trusting demo R.
3. Update learnings (`add_learning` / `update_learning`), citing the numbers and where they came from (`provenance`). Give each a `kind`, and give statistical ones `n`, `days` and `sample`. Work through `research_report.learnings` (the brief's "learnings to re-check" section): `review_due`, `anecdote` (supported/adopted on n < 30), `pre_episodes` (statistics from before 2026-10-01: re-state n on the episode basis) and `?` (inferred) fields. Changing an adopted rule's status is a decision for you or the owner, not for the flag.
4. Curate the rulebook (docs/RESEARCH.md "The rulebook and search"). The brief loads only settled learnings; the rest is searched with `learnings`.
   - `missing_rule`: write the one-line `rule` (≤ 160 chars: the condition and the action).
   - `harmful`: the trades that cited it lost (outcome scores, never votes). Re-check the claim with data, then supersede or reject it.
   - `expired`: extend `valid_until`, supersede or reject it.
   - `similar` (returned by `add_learning`): when two learnings say the same thing, keep one and supersede the other.
   - Supersede instead of rewriting: a changed claim is a new learning plus `superseded_by` on the old one. The statement never changes.
   - Set `valid_until` on regime- or one-coin claims (a day's tell, a coin's current behaviour).
   - **Doctrine and skills follow the journal** (docs/CONTEXT.md). When the brief's re-check section names a learning that `docs/DOCTRINE.md` or a skill cites but the journal doesn't hold as settled, re-state its sample (`update_learning`), or reword or drop the rule in that file. A rule enters the doctrine only with a settled learning's id; write these changes in this review, not after each trade.
5. `golive_report` (`.venv/bin/shortbot golive --text`): the go-live gate on the demo evidence (docs/GO_LIVE.md). Each blocker names the evidence that is missing: forward tests, independent trades, expectancy at mainnet prices, drawdown, uptime, incidents, slippage calibration, plumbing drills. Report it to the owner. READY never means "switch": only the owner edits `.env` or goes live.

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
Also available: `lab.ohlcv`, `lab.oi_history`, `lab.funding_history`, `lab.taker_ratio`, `lab.tickers`, `lab.snapshot`, `lab.metrics` (guardian history), `lab.trades`, `lab.report`, `lab.get(path, params)` (any public endpoint), plus the indicators `lab.rsi/ema/atr/vwap_bands`. The module is read-only: no orders.

**Scripts never call fapi.binance.com directly** (no urllib, requests, httpx or curl, no thread pools of raw calls). Use `shortbot.lab` or `shortbot.http.get(path, params)`: both go through the host-wide weight guard, paced, so a study can't spend the IP's budget the guardian needs (a raw script got the IP banned for 14 min on 2026-10-07). Bulk history comes from data.binance.vision (`shortbot.histdata`, `data/hist.db`), which costs no API weight (docs/SCANNING.md "Weight guard").

## Acting on findings
Settle each change with the data, not by asking the owner which option they prefer: run the study, record it, change it openly, then tell the owner what changed and why. Ask only on a genuine risk-appetite trade-off, with each option quantified. Code changes need tests (`.venv/bin/pytest -q`).
- **A threshold looks wrong**: open a named experiment with `set_risk_params` (risk limits), or change the signal in `signals/*.py` yourself (score points and vetoes): tests green, commit, push; record the evidence.
- **A new signal**: write one `@signal(...)` function in `src/shortbot/signals/` (data via the MarketContext, which adds a getter in `market/hub.py` if a new feed is needed). Its features are logged and reported automatically. Give it points or vetoes only when the data supports it (`research_report.by_feature_tertile`).
- **A new tape state**: add one `TapeRule` in `src/shortbot/tape.py`.
- **A new setup**: trade it as `exp_<name>` first. Once the evidence holds, register it in `setups.py` with a detector and default plan, and add a skill in `.claude/skills/<name>/SKILL.md`.
- **A veto costs more than it saves**: `add_learning` with the numbers, then relax it in the code, openly and with that evidence. Single trades can bypass it with `veto_override`.
- **Retire** a setup with `status="retired"`. History is kept.

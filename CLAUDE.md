# shortbot: AI-assisted short-only Binance futures research desk

- Trading: use the `trading-session` skill. Reviewing what we learned or changing rules: `research-review`. Extending the code: `docs/RESEARCH.md`.
- Pipeline: `market/` DataHub (data) → `signals/` plug-ins → `tape.py` classifier → setups/plans → trading/entries → guardian. New logic goes into that layer, not into the scanner.
- The exchange is touched only through the `shortbot` MCP tools. Ad-hoc analysis goes through `shortbot.lab` (read-only), via Bash.

## Rules that aren't obvious
- Never work around a rejection (split orders, sneak limits up). Change it openly instead:
  - risk limits: `set_risk_params` with an experiment tag
  - a single trade: `veto_override` with a research reason
  - either way, tell the human.
- Every entry needs a plan (trigger, invalidation, targets, time stop, auto flags). Every exit needs a lesson. Conclusions go into `add_learning` with evidence, not into chat.
- The AWS guardian always uses the `.env` baseline. AI parameter overrides apply in demo only.
- Only the human edits `.env` or switches to live.

## Dev
- `.venv/bin/pytest -q`. Files stay under 400 lines.
- `tests/fakes.py` mirrors real ccxt/Binance response shapes. When live behaviour differs, fix the fake first.

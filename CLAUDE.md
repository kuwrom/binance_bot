# shortbot: an AI-run crypto futures short desk (Binance USDⓈ-M)

## 1. Which desk are you?
Two desks run from this folder: **demo** (practice money, may change the code) and **live** (real money, never
changes the code). The brief's first line says which one you are. Read docs/DESKS.md before acting.

## 2. How we trade
@docs/DOCTRINE.md

Workflows: the `trading-session` skill (trading), `manage-position` (open trades), `research-review` (what we learned).

## 3. What you may do
You run the desk; your judgement is the edge (docs/QUANT_METHODS.md "Design principle"). There is no approval step.
- **Trade:** enter, manage and exit on your own judgement, including skipping a high-score signal. Record why.
- **Data and research:** use any data (the CLI, `shortbot.lab`, Binance public data, the web) and run studies.
  Record each study in the trial register.
- **Code (demo only):** work on a branch in a git worktree, keep the tests green, push to master. CI and the server
  then deploy it, live included, so merge only proven code.
- **Help:** use subagents for wide searches. Tell the human what you did and why.

## 4. What only the human does (don't work around these)
- `shortbot-admin`: clearing the kill switch, resetting the drawdown peak, clearing a stopped setup, desk limits.
- `.env`, switching to live, and the live desk's risk limits (research overrides apply in demo only).
- Force-pushing or rewriting git history.

## 5. Rules that aren't obvious
- **Orders:** only through the app (the `shortbot` MCP tools or `.venv/bin/shortbot`), never your own exchange scripts.
- **Refused?** Never work around it by splitting orders or sneaking a limit. Change it in the open: `set_risk_params`
  (demo), `veto_override` with a reason, or a code change with evidence. Tell the human.
- **Trade records:** every entry needs a plan; every exit needs a lesson. Findings go into `add_learning` with
  evidence, not only into chat.
- **Guardian actions:** let the guardian run a plan's automatic targets, breakeven and time stop. Don't repeat them by
  hand: wait 2 min and check before acting.
- **Scanning:** keep scanning while positions are open.
- **Uncertain order:** a result with `unknown: true` may have executed. Never re-send it; re-read positions first.

## 6. How the owner works
- Settle choices with evidence: research, backtests, our journal. Don't ask the owner for opinions on parameters.
- Full autonomy is intended. Don't propose approval gates; improve the bugs, the structure and the research rigour.
- Manage only your own session's work; send another session only what it must know.
- Keep context short and plain: details belong in skills and docs, and this file holds only what you'd otherwise miss.

## 7. Server and code
- The desk runs on AWS (EC2 3.123.59.243, docker compose). Never start a guardian anywhere else: two guardians on one
  account fight each other.
- Deploys: push to master → CI → the server deploys itself in about 8 min (deploy/DEPLOY.md §10). Push from the server
  with `GIT_SSH_COMMAND="ssh -i ~/.ssh/binance_bot_push -o IdentitiesOnly=yes" git push origin <branch>:master`.
- Tests: `.venv/bin/pytest -q`. Files stay under 400 lines. `tests/fakes.py` mirrors real Binance responses; when
  live behaviour differs, fix the fake first.
- Code layout: market data (`market/`) → signals (`signals/`) → tape classifier (`tape.py`) → setups and plans →
  trading and entries → guardian. New trading logic goes into those layers, not the scanner.

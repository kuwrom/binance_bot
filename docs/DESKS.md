# Two desks: live and demo (owner decision 2026-10-04)

Two Claude Code sessions can run at once, one per desk. Each has its own checkout, `.env`, journal, guardian and gateway.
Check which one you are in first: the brief's header, or `.venv/bin/shortbot account` → `"mode"`.

| | demo desk | live desk |
|---|---|---|
| checkout | `~/binance_bot` (branch `deploy`, autodeploys, §10) | `~/binance_bot_live` (branch `deploy`, updated by hand) |
| money | Binance Demo Trading | real |
| compose | guardian, gateway, web console, Caddy | guardian and gateway only (`docker-compose.override.yml`) |
| limits | `.env` baseline plus AI research overrides (`set_risk_params`) | `.env` baseline only (`AI_PARAMS_IN_LIVE=false`) |
| mandate | the full one in CLAUDE.md "You are in control" | the narrower one below |

## The demo desk
Everything in CLAUDE.md: trade, experiment, change and ship the app, run studies, override vetoes with a research
reason. New setups, parameter experiments and code changes are proven here first.

## The live desk
Real money. Trade what is already established; research freely, but outside the app.
- **Trade established setups only.** An entry relies on rulebook learnings that are supported or adopted with a
  positive edge. Cite them in the plan's thesis as `demo#<id>`: `plan.uses_learnings` only accepts ids that exist in
  the live journal. A setup the evidence says loses (e.g. pump_fade, #51) or that has
  no settled learning is a demo trade, not a live one. When in doubt, skip and log why.
- **Baseline size.** No `set_risk_params` (refused in live anyway) and no `budget_override`. Start at the minimum
  size; the first 30 live trades are probation (docs/GO_LIVE.md).
- **No veto overrides.** A veto that blocks a live trade is declined. If it looks wrong, say so to the demo desk
  with the evidence; it gets tested there.
- **No core code changes.** Don't edit, commit or push from `~/binance_bot_live`. Fixes and features are made
  by the demo desk (or a worktree of `~/binance_bot`), go through CI and reach live only when the live checkout
  is updated.
- **Temporary scripts are fine.** Studies, data pulls and checks go in the session's scratchpad and use
  `shortbot.lab`, the CLI and public data. Record the trials as usual.
- **Every entry needs a plan and every exit a lesson**, as on demo. The kill switch and all human-only walls
  apply unchanged.
- **Updating the code:** only when the live desk is flat (no positions, no resting entries):
  `git pull --ff-only` in `~/binance_bot_live`, `uv sync --locked --extra dev`, `.venv/bin/pytest -q`, then
  `docker compose up -d --build`; reconnect `/mcp`. Tell the human which commit is now running.

## Which data counts as evidence on live
The demo journal mixes evidence with drills, probes and experiments. Its tags tell them apart; a live decision
rests only on the first column.

| table | counts | doesn't count (not live evidence) |
|---|---|---|
| `learnings` | `status` supported or adopted, `kind` statistical, `n` ≥ 30, n stated in episodes | `hypothesis`, `rejected`; n < 30 (the brief lists them, e.g. #11 n=1, #80 n=18); statistics from before episode counting (the brief's re-check list) until re-stated; `setup` single_coin_replay |
| registry (`shortbot registry forward`) | `live` nodes, forward test passed | `draft`, `shadow` (forward test still collecting), `probation`, `retired` |
| `trades`, `entries` | `mode` live | `setup` exp_* (experiments) and exp_drill (plumbing drills); demo P&L as expected live P&L: demo fills differ (GO_LIVE.md) and `experiment` tags such as lev10_risk3 sized above the baseline |
| `pattern_episodes` | `source` live (forward) | `source` bt60, week_study, forensics: in-sample, already used to build the rules |
| `decisions`, `intents`, `alerts`, `guardian_gaps` | the rest | `actor` cli:drill:*, cli:probe, `tag` drill: tests of the plumbing, not of the market |

As of 2026-10-04 no registry node is live-eligible (all shadow or worse, forward n = 0), so no setup has passed
the desk's own promotion rule. Settled positive evidence exists only for event setups (listing_fade #15 #36,
unlock #44); pump_fade's is negative (#51).

## Shared knowledge
The rulebook lives in the demo journal. The live session reads it from the demo checkout
(`cd ~/binance_bot && .venv/bin/shortbot learnings ...`) and records conclusions there too (`shortbot learn`,
evidence prefixed `[live]`), so both desks learn from one register. Live trades, decisions and alerts stay in the
live journal.

## Talking to the other desk
Sessions reach each other with `SendMessage` (`ListAgents` shows the names). Keep it short: a finding, a veto that
looks wrong, a coin both are about to trade, a deploy that needs `/mcp`. Don't manage the other desk's trades.

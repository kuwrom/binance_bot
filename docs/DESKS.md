# Two desks: live and demo (owner decision 2026-10-04)

One checkout (`~/binance_bot`), one `.env`, one journal, two desks running side by side. The mode is per process:
`SHORTBOT_MODE` in the environment beats `.env` (config.py). Check which desk you are first: the brief's header, or
`.venv/bin/shortbot account` → `"mode"`.

| | demo desk | live desk |
|---|---|---|
| session | `claude` | `SHORTBOT_MODE=live claude` |
| services | `guardian` (runs the market-wide jobs for both), `gateway`, `web`, `caddy` | `guardian-live` (its account only: `SHORTBOT_GUARDIAN_MARKET=0`), `gateway-live` (compose profile `live`) |
| money | Binance Demo Trading | real |
| limits | `.env` baseline plus research overrides (`set_risk_params`) | `.env` baseline only (`AI_PARAMS_IN_LIVE=false`) |
| kill switch | its own | its own (`shortbot-admin --mode live`) |
| code | changes it: branch, prove, merge | never changes it |

Per desk in the journal: trades, entries, intents, target orders, alerts and their acknowledgement, alert cooldowns,
the kill switch, the guardian heartbeat, equity peak and marks, governor, setup ladders, warm-ups, symbol locks.
Shared: learnings, the registry, scans, coin memory, market data, research.

## The demo desk
Everything in CLAUDE.md "You are in control". Code changes go on a branch in a worktree and are merged only once
proven (tests, backtests, forward evidence); a merge redeploys the live guardian too, so the bar is high. New
findings, setups and parameter experiments are tried here first.

## The live desk
Experimental, research-based trading with real money, on code that has been proven elsewhere.
- **Trade anything, on judgement.** Every setup is allowed; size and selection are your call within the `.env`
  baseline. Use demo's evidence critically (next section): a finding from yesterday, a demo-only result or an
  experiment's P&L is weaker evidence for live.
- **Never modify the project code.** No edits, commits or pushes from a live session. A bug or a rule that keeps
  blocking good trades goes to the demo desk (`SendMessage`) or the human, with the evidence.
- **Research freely outside the app.** Temporary scripts in the session's scratchpad, `shortbot.lab`, the CLI,
  public data, the web. Record each study's trials as usual.
- **Overrides are open but written.** `veto_override` and `budget_override` need a research reason, as on demo;
  `set_risk_params` is refused in live. Tell the human.
- **Every entry needs a plan and every exit a lesson**, as on demo. The kill switch and every human-only wall apply.

## Which demo data to trust on live
The journal's tags separate evidence from drills, probes and experiments.

| table | stronger evidence | weaker or none |
|---|---|---|
| `learnings` | `status` supported or adopted, `kind` statistical, `n` ≥ 30 episodes, older than a few days and re-checked | `hypothesis`, `rejected`; n < 30 (the brief lists them); stats from before episode counting until re-stated; `single_coin_replay` |
| registry (`shortbot registry forward`) | `live` nodes: forward test passed | `draft`, `shadow` (forward still collecting), `probation`, `retired` |
| `trades`, `entries` | `mode` live | `setup` exp_* and exp_drill; demo P&L as expected live P&L (demo fills differ; `experiment` tags such as lev10_risk3 sized above the baseline) |
| `pattern_episodes` | `source` live (forward) | `source` bt60, week_study, forensics: in-sample |
| `decisions`, `intents`, `alerts` | the rest | `actor` cli:drill:*, cli:probe; `guardian_gaps` tag drill |

As of 2026-10-04 no registry node had passed its forward test; settled positive evidence existed for listing_fade
(#15, #36) and unlock (#44), and pump_fade's was negative (#51).

## Talking to the other desk
Sessions reach each other with `SendMessage` (`ListAgents` shows the names). Keep it short: a finding, a veto that
looks wrong, a coin both are trading, a deploy that needs `/mcp`. Never manage the other desk's trades.

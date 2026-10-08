# Two desks: live and demo (owner decision 2026-10-04)

One checkout (`~/binance_bot`), one `.env`, one journal, two desks running side by side. The mode is per process:
`SHORTBOT_MODE` in the environment beats `.env` (config.py). Check which desk you are first: the brief's header, or
`.venv/bin/shortbot account` → `"mode"`.

| | demo desk | live desk |
|---|---|---|
| session | `claude` | `SHORTBOT_MODE=live claude` |
| services | `guardian` (runs the market-wide jobs for both), `gateway`, `web`, `caddy` | `guardian-live` (its account only: `SHORTBOT_GUARDIAN_MARKET=0`), `gateway-live` (compose profile `live`) |
| money | Binance Demo Trading | real |
| limits | desk baseline plus research overrides (`set_risk_params`) | desk baseline only (`AI_PARAMS_IN_LIVE=false`) |
| desk limits (human) | `shortbot-admin limits set ...` | `shortbot-admin --mode live limits set ...` |
| kill switch | its own | its own (`shortbot-admin --mode live`) |
| code | changes it: branch, prove, merge | never changes it |

**Risk limits per desk** (owner decision 2026-10-04, `desk_limits.py`). Each desk's baseline resolves as built-in
defaults -> `.env` -> that desk's limits in the journal; research overrides go on top on demo only (bounded by
`BOUND_*`). The guardian, its loss trips and governor, the risk budget and every placement check use the baseline.
Only the human sets desk limits, in a real terminal: `shortbot-admin [--mode live] limits status`, `limits set
MAX_LEVERAGE=2 MAX_TOTAL_EQUITY_X=1.5 --reason "..."`, `limits unset KEY --reason "..."` (code typed on the tty,
journalled and alerted as `desk_limits_changed`). Nothing in the MCP server or the `shortbot` CLI writes them. With
none set, `.env` applies unchanged. `max_position_equity_x` / `max_total_equity_x` cap the per-coin / total notional
at that multiple of current equity, whichever of it and the USDT cap is lower. `shortbot params`, `risk_params` and
the brief show each limit with its source.

Per desk in the journal: trades, entries, intents, decisions, target orders, alerts and their acknowledgement, alert
cooldowns, the kill switch, the guardian heartbeat, equity peak and marks, governor, setup ladders, warm-ups, coin
events seen, symbol locks, desk limits. Shared: learnings, the registry, scans, coin memory states, market data, research, and the
market jobs' alerts (scan, processes, social, market news, listing heads-ups), which both sessions see.

**Live depends on the demo guardian.** It runs the market jobs for both desks: the market map and its pause veto,
scans, liquidations, streams, calendar. The live guardian protects live positions on its own, but while `guardian` is
down live's market-pause veto reads nothing; it alerts `market_feed_stale` (urgent). Don't stop the demo stack while
live trades. One guardian per desk: a second one for the same mode refuses to start (`data/run/guardian-<mode>.lock`).

**The web console is demo's** (alerts, kill switch, positions). Follow live through its session, Telegram (`[live]`)
and `shortbot-admin --mode live status`.

## The demo desk
Everything in CLAUDE.md section 3 ("What you may do"). Code changes go on a branch in a worktree and are merged only once
proven (tests, backtests, forward evidence); a merge redeploys the live guardian too, so the bar is high. New
findings, setups and parameter experiments are tried here first.

## The live desk
Experimental, research-based trading with real money, on code that has been proven elsewhere.
- **Trade anything, on judgement.** Every setup is allowed; size and selection are your call within the desk
  baseline (the human's live desk limits over `.env`). Use demo's evidence critically (next section): a finding from yesterday, a demo-only result or an
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

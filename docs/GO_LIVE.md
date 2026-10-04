# Go-live gate (#46)

`.venv/bin/shortbot golive --text` (MCP `golive_report`, web console Research) answers **READY** or **NOT READY** from the demo journal, and names every blocker with its evidence. It only reads: switching to live is the owner's, in `.env` (deploy/DEPLOY.md §6). The session brief shows the same verdict on one line, `go-live: NOT READY — <n> blockers: …`, from a light check that skips the drawdown bootstrap and uses its floor instead, so it is never looser than the full report.

Code: `golive.py` (the criteria), `golive_view.py` (plumbing, text, brief line, live probation).

## Runbook
1. **Plumbing, once a month** (every result counts for 30 days; deploy/DEPLOY.md §7a has the details):
   - `.venv/bin/shortbot backup drill`, any morning after the nightly copy. For the off-box half, restore a copy from the bucket on another machine, then `.venv/bin/shortbot drill attest backup-offbox --evidence "<file, from where, what you checked>"`.
   - With the desk flat: `.venv/bin/shortbot drill kill-switch`. The owner clears the switch in their own terminal (`.venv/bin/shortbot-admin clear --reason '...'`), then `.venv/bin/shortbot drill kill-switch --verify` records the pass.
   - With no resting entry, on the desk host, no push meanwhile: `.venv/bin/shortbot drill restart --phase before`; `docker compose stop guardian` for at least 11 min; after 10 min `.venv/bin/shortbot kill-switch` (the watchdog); `docker compose start guardian`; `.venv/bin/shortbot drill restart --phase after` (run it again if it answers `pending`).
   - `.venv/bin/shortbot probe --all` (demo, a clean symbol). A probe whose answer differs from the fake: fix the fake first (tests/fakes.py), re-run the probe; if the difference is understood and harmless, record why with `.venv/bin/shortbot log probe_explained "<probe>: <why, and the fix>"`.
   - `.venv/bin/shortbot drill status` lists the latest results and the uptime summary.
2. **Trade the demo desk** with full plans until the sample criteria pass. Drill trades (setup `exp_drill*`) never count.
3. **Run the gate**: `.venv/bin/shortbot golive --text`. Work each blocker from its `detail`.
4. **READY**: the owner follows deploy/DEPLOY.md §6 and **starts at minimum size**.
5. **Live probation**: the first 30 live trades. In live mode the report adds `live probation: n/30 live trades`, the live fills' excess over the backtest's slippage model, next to demo's, and the live k. The gate itself keeps reading the demo evidence.

## Window and sample
- Window: from max(2026-10-03, kv `uptime_since`). 2026-10-03 is when the guardian and journal moved to EC2. The Mac-era demo ran another stack and has no uptime record. `uptime_since` is the first verified heartbeat under the uptime code.
- Sample: the closed demo trades opened in the window (`Journal.closed_results`: never a drill's). The entry day is the day cluster.
- In live mode the report reads the same journal as its demo reader. Live trades feed only the probation line.

## Criteria (gated)
Each criterion reports `ok`, `value`, `threshold`, `source`, `summary` and `detail`.

| criterion | passes when | evidence | source of the threshold |
|---|---|---|---|
| registry | ≥ 1 traded setup is live-eligible: its strategy node passed its frozen forward test and is not retired or on probation | `registry_forward.forward` per setup (via `read.SETUP_STRATEGY`) | the only promotion rule (docs/REGISTRY.md, #26): 30 trades over ≥ 20 days, one-sided day-clustered t, p ≤ 0.025 / M |
| sample | n_eff ≥ 30 and ≥ 20 entry days | two-way day × coin clustered variance of mean R, Kish n_eff (`cluster_se.mean_inference`) | 30 = `registry_forward.MIN_TARGET_N` (n < 30 is an anecdote); 20 = `research_stats.MIN_DAYS` (Cameron & Miller 2015 §VI.A: fewer clusters, descriptive only) |
| expectancy | lower 95% bound of mean R > 0, on the worse of demo R and mainnet-repriced R | two-way clustered se, t(min(days, coins) − 1). Repricing: each trade's R plus its fills' `excess_bps` (fill_report, #35) × notional / risk | Cameron, Gelbach & Miller 2011; the demo venue is not mainnet, so the worse of the two counts |
| stress | mean R > 0 at 1.5× fees and 2× slippage | each trade at the worse of demo and mainnet R, minus 0.5× its fees and one more model slippage charge | `backtest/run.py` STRESS; docs/BACKTEST.md: SURVIVES needs stress R > 0 |
| drawdown | each setup's max drawdown of cumulative R ≤ its ladder halve level | `backtest/drawdown.max_drawdown` per setup, in close order | `setup_ladder.thresholds`: the 80% quantile of the 100-trade max DD of a +0.10R setup (Bailey & López de Prado 2015; docs/PROPOSALS.md P2) |
| equity | drawdown of the daily equity marks < `dd_halve_pct` (8%) | kv `eq_mark:day:*` (the first equity of each UTC day, equity_marks.py) | the governor's halve tier (docs/RISK_GOVERNANCE.md, .env baseline) |
| uptime | uptime recorded; no untagged gap, nor one open now, > 600 s | `guardian_gaps` (uptime.py) and the heartbeat's age | `killswitch.STALE_HEARTBEAT_S`: past it the watchdog trips HALTED. Drill-tagged gaps are planned |
| incidents | 0 alerts of NAKED_POSITION, stop_missing, stop_restore_failed, untracked_position, unexpected_long, position_flip | the alerts table in the window. Near-misses (stop_restored, stop_failed, forced_close, bridge_*, stop_left, widen_undone, close_incomplete/unknown, fill_unresolved, orphan_stop_*) are listed, not gated | a position without its stop, or one the desk didn't open (guardian_positions.py, stops.py) |
| slippage | `fill_report.calibrate` ok: ≥ 30 trades with market or stop fills over ≥ 20 days, k > 0 | k refit from the desk's own fills vs the model's 20 bp | fill_report (#35) |
| plumbing | each drill's latest result is a pass; each probe matches its fake (or is explained); the backup drill passed; the gateway soak is clean. Each within 30 days | `drill_result` / `probe_result` / `probe_explained` decisions, kv `backup_drill_last_ok`, kv `gateway_heartbeat`, the intents table | plan-46 §3b; DEPLOY §7a; docs/GATEWAY.md D6 (no breakglass or unknown rows left unexplained) |

Plumbing details:
- **Drills:** kill-switch, restart, backup, backup-offbox (attested). `awaiting_clear` is not a pass.
- **Probes:** every probe in `probes.ORDER`, latest result ok and clean, with `matches_fake` true. `user-stream` is observational (it reports which URL got which events), so ok and clean are enough. An inconclusive, mismatched or unclean result counts once a later `probe_explained` decision starts with the probe's name.
- **Soak** (last 30 days, this mode): the gateway has run. There is no `breakglass:*` intent outside a drill window, no `unknown` or `interrupted` intent, and no intent open past `intents.MAX_OPEN_S`.

## Reported, not gated
- **parameters:** how many window trades were sized under the `.env` baseline the live guardian uses (`entry_features.limits`), which fields differed, and the experiments behind them. AI overrides apply in demo only (CLAUDE.md), so demo R under other limits is weaker evidence for live.
- **Near-misses** (incidents' detail) and the uptime totals (downtime, drill-tagged gaps).

## Exchange behaviour behind the plumbing (Binance USDⓈ-M docs, checked for #46)
- Error codes, https://developers.binance.com/docs/derivatives/usds-margined-futures/error-code: -1006/-1007 mean the execution status is unknown. `errors.py` classifies -1000/-1001/-1006/-1007/-1008 as OutcomeUnknown: looked up by client id, never resent.
- General info, https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info: an HTTP 503 "Unknown error" is unknown (verify before a retry), "Service Unavailable" is a failure, and -1008 (throttled) is a failure (reduce-only and closePosition are exempt). The desk treats Unavailable and -1008 as unknown: safe, at the cost of a 90 s grace. The 503 body format can't be probed.
- Query Order / Query Algo Order find an order by `origClientOrderId` / `clientAlgoId`. They answer not found once the order is CANCELED or EXPIRED unfilled and 3 days old, or 90 days old; -4116 is a duplicate id. Probes 8, 9 and 11 verified this on demo.
- User data stream: a listenKey is valid for 60 min, a POST extends it, and a connection lives 24 h. After `listenKeyExpired` no events arrive until a new key; the `/private` path has been in use since 2026-04-23. Order Update execution types: NEW, CANCELED, CALCULATED, EXPIRED, TRADE, AMENDMENT (user_stream.py, user_events.py; probe 7).
- Demo is not live: the futures docs say nothing about demo fills. fill_quality.py and fill_report.py measure the gap, and the gate reprices demo R at mainnet prices.

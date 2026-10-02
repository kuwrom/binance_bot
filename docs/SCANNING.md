# How to scan (research 2026-10-02)

The question: should we poll every coin every minute, or is there a better way? The answer is a tiered, event-driven scanner:
- free websocket streams watch every perp;
- cheap local triggers decide what deserves a look;
- full signal scoring runs only on those coins;
- the agent wakes only on alerts.

## What we measured

**Binance's limit:** 2,400 request-weight per minute per IP (the REST `exchangeInfo` `rateLimits`). Overruns earn 429s and escalating 418 IP bans.

**Today's scan is REST-only:**
- One bulk ticker call ranks all 460 coin perps by 24h change.
- The top ~25 movers are then deep-scored.
- That is 325 requests and ~800 weight per pass (48 s): 168 klines calls (several timeframes per coin), 42 OI-history, 25 mark-price klines, 21 depth, 21 taker-ratio, 21 premium klines and 21 funding.

| poll cadence | share of the IP budget |
|---|---|
| every 5 min | ~7% |
| every 1 min | ~33% continuously, before the guardian and the desk |

**Streams cost zero REST weight.** Measured 2026-10-02 on `wss://fstream.binance.com/market/stream`:

| stream | rate | carries |
|---|---|---|
| `!ticker@arr` | ~1 msg/s, ~150 changed symbols per msg | 24h change, last, high/low, volumes |
| `!markPrice@arr@1s` | 1 msg/s, all 472 contracts | mark, index (so premium), funding rate, next funding time |
| `<sym>@kline_15m` (any interval) | live, `x` flags the close | OHLCV plus taker-buy volume, so local candles and the taker ratio need no REST |
| `!forceOrder@arr` | at most 1 order per symbol per s | liquidations (the guardian already records it) |

Only open interest, the long/short ratios and depth still need REST: about 6-8 weight per coin.

**The universe:**
- 460 coin perps;
- 183 with 24h volume ≥ $5M;
- 91 with ≥ $20M;
- only 24 moving ≥ 10% in 24h right now.
- In the 60-day backtest every strategy LOSES on coins with OI < $10M (t −2.0 to −2.8; docs/BACKTEST.md), so full scoring belongs to the liquid ~180, while the streams still watch everything for events.

**How fast signals go stale** (decay.HALF_LIFE_H priors):

| signal | half-life |
|---|---|
| order flow | ~10 min |
| sweeps, liquidations | ~45 min |
| social | 4 h |
| funding/OI | 16 h |
| pump | 36 h |
| listing | 10 d |
| momentum | 14 d |

The setups trigger on 15m and 1h closes. A fixed 1-minute poll re-reads the same bars; what needs speed is the 10-45 minute events, and those arrive on streams.

**What a 24h-movers scan misses:**
- fresh 1h/4h moves that aren't top-24h movers yet;
- funding and premium extremes on non-movers (crowding, the funding cards);
- liquidation cascades;
- calendar and announcement events: listings, delistings, unlocks, hacks.

The research says this is where the edge is: events, flows and crowding (docs/strategies/README.md).

## Design
| tier | what | universe | cost |
|---|---|---|---|
| 0 Streams | `!ticker@arr`, `!markPrice@arr@1s`, `!forceOrder@arr`, plus `kline_15m` / `kline_1h` for the liquid universe → local rolling candles and taker flow | all 460 (klines: the ~180 liquid) | 0 weight |
| 1 Triggers | evaluated locally at every 15m close AND immediately on fast events | all liquid coins | CPU only |
| 2 Deep score | the existing signal stack, with candles from the local cache; REST only for OI history, ratios and depth | only coins that triggered, ~5-20 per 15m | ~6-8 weight/coin, < 1% of the budget |
| 3 Agent | alert only on tradable A+ candidates (scan_job.pick_alerts) | a few per day | LLM time |

**Tier 1 triggers:**
- a 1h or 4h move z ≥ 2 versus the coin's own volatility;
- a volume surge;
- premium or funding in the top/bottom decile of the universe;
- a liquidation burst;
- a new 15m lower high after a pump;
- listing age < 30 d;
- an announcement or unlock date (guardian jobs).

**Fast events wake tier 1 at once,** because their edge decays in 10-45 minutes:
- a liquidation burst;
- a premium spike;
- a BTC 15m impulse, which is also the market-pause veto.

**Logging:** every tier-2 evaluation is logged as a research candidate, exactly as today. Tier-1 trigger counts are logged too, so the triggers themselves can be evaluated later.

**Interim:** until tier 0/1 exist, `scan_job.run` every 5 min (EVERY_S = 300) is the safe REST version.

## Weight budget: protective first
One IP budget (2,400/min) is shared by:
- the guardian's protective lane (positions and stops every 60 s, entries every 5 s);
- watch;
- ADL (5/min when positioned);
- the desk's MCP calls;
- research.

A 429 or a -1003/418 ban blinds the stop checks, so research must never be the call that tips it.

**Rules:**
1. **Every client reads `x-mbx-used-weight-1m` on each response.**
   - Research jobs (scan, tier 2, labelling) pause while used weight > 50% (1,200).
   - They also pause for 2 minutes after any 429.
   - The protective lane never waits.
2. **Tier 2 caps itself** at ~150 weight per 15m window (about 20 coins). Excess triggers queue to the next window, highest trigger score first.
3. **Streams first:** with candles from tier 0, a deep score is ~6-8 weight instead of ~30. Research falls from ~800 per pass to < 1% of the budget, leaving the rest to the protective lane.

### Weight guard spec (task #70)
**Track weight per host**, from each response's `x-mbx-used-weight-1m`, keyed by the request's host. A host's budget is its `exchangeInfo` REQUEST_WEIGHT limit.
- **Demo today:** the trade client (`ex.trade`, demo-fapi) and `ex.public` (mainnet fapi) are separate budgets.
- **Live:** both hit fapi.binance.com from one IP. That shared budget is where the guard matters.
- `data.binance.vision` (the archives) is a CDN, not API weight: never guarded.

**Callers are tagged by class.** A contextvar is set by the caller; ccxt doesn't need to know.

| class | rule | callers |
|---|---|---|
| protective | never waits | the guardian's PROTECTIVE lane (entries, positions, entry_reconcile); WATCH; margin/ADL checks; kill-switch sweeps and flatten; every MCP/CLI order, close, stop and amend path (trading*, entries*, positioning) |
| desk | waits only above 80%, and never after it has already started an order sequence | MCP/CLI reads the analyst needs before acting: positions, account, coin_snapshot, read_coin, governor/budget reads |
| research | yields above 50%, and for 2 min after any 429, -1003 or 418 on that host; tier 2 also has its own ~150 weight per 15m cap | the scanner (scan_shorts, scan_job, tier 2 deep scores); guardian MONITOR/RESEARCH lanes (metrics, research/labelling, market_map, sweeps, supply, laggard, calendar, social, refresh); `shortbot.lab`; histdata REST tails; the backtest/refresh scripts |

**Yield means wait, not drop:** check every ~2 s for up to the job's timeout, then raise a `budget_wait` error that the lane records. It must never consume a protective call's turn.

**Tests:**
- a fake client returns rising weight headers: research calls block above 1,200 while protective calls pass;
- a 429 on one host doesn't block the other host;
- demo's split hosts are tracked separately.

**Built (`weight.py`, `exchange.GuardedUSDM`):**
- **Where it hooks:** each response is read in ccxt's `on_rest_response`, and each request is admitted in `fetch`, before it is sent. ccxt's `last_response_headers` is one attribute per client that concurrent calls overwrite, so it isn't used.
- **No resent 429s:** ccxt's own retry loop never resends a 418/429. The public client's `maxRetriesOnFailure` used to resend a 429 three times within 3 s, which is the "failing to back off" that earns a ban.
- **Where the class is set:**
  - each guardian lane in `Lane._run` (a job's deadline is its timeout − 5 s), with governor and margin_mode protective;
  - MCP tools in `mcp_server.PROTECTIVE_TOOLS` / `RESEARCH_TOOLS`;
  - CLI commands in `cli.PROTECTIVE_CMDS` / `RESEARCH_CMDS`;
  - admin is protective; lab and histdata are research.
- **Writes:** any write (non-GET) makes the rest of its scope protective.
- **Shared locks:** markets reloads under `Exchanges`' shared locks always run as protective.
- **Measured limits (2026-10-02):**
  - fapi.binance.com allows **2,400**/min; demo-fapi allows **6,000**/min. Each host's own exchangeInfo sets its limit.
  - `x-mbx-used-weight-1m` arrives lowercase.
- **Ignored readings:** responses from `ticker/bookTicker` and `ticker/price`. The official SDK calls their header inaccurate.
- **Reading lifetime:** Binance doesn't document whether the minute is fixed or rolling, so a reading is trusted for 60 s.
- **Follow-ups:**
  - the state is per process: the MCP server, CLI, guardian and lab learn the IP's usage only from their own responses. Sharing the last reading through kv is a follow-up.
  - `lifecycle_data.py` calls fapi over raw httpx, so it is not guarded.

## Build (who)
1. **Stream collector + local candle store** (`market/streams.py`): tickers, mark/premium/funding, klines for the liquid universe, a reconnect at 24h, SQLite. It is a guardian process like `LiquidationRecorder`, so binance-bot-99 wires it.
2. **Trigger rules** (`scan_triggers.py`, pure and tested), each logged with its own counts for later evaluation.
3. **DataHub reads candles from the local store** when fresh, falling back to REST.
4. **The guardian research lane** runs triggers at each 15m close plus on events, deep-scores the triggered coins and alerts through scan_job.

## Not verified (docs site renders client-side)
- the per-connection stream cap and message-rate limits;
- the connection lifetime (24h is common practice).

Design for ≤ 200 streams per connection, reconnect daily, and check the live docs when building.

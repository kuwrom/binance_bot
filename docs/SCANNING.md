# How to scan (research 2026-10-02)

The question: should we poll every coin every minute, or is there a better way? The answer is a tiered, event-driven scanner:
- free websocket streams watch every perp;
- cheap local triggers decide what deserves a look;
- full signal scoring runs only on those coins;
- the agent wakes only on alerts.

## What we measured

**Binance's limit:** 2,400 request-weight per minute per IP (the REST `exchangeInfo` `rateLimits`). Overruns earn 429s and escalating 418 IP bans.

**Before #77 the scan was REST-only** (today's tiers are below):
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

Only open interest, the long/short ratios and depth still need REST: about 6-8 weight per coin, if every kline came from streams. Measured (#72): ~29 per coin today (see "Tier-2 cost, measured").

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
| 2 Deep score | the existing signal stack, with candles from the local cache; REST only for OI history, ratios and depth | only coins that triggered, ~5-20 per 15m | estimated ~6-8 weight/coin; measured ~29 (#72), ≤ 600 per 15m, ~1.7% of the budget |
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

**Interim:** before tier 0/1 existed, a REST scan every 5 min was the safe version. The guardian now runs the tiered scan ("Wired (#72)"); the REST scan was removed in #77.

## Weight budget: protective first
One IP budget (2,400/min) is shared by:
- the guardian's protective lane (positions and stops every 60 s, entries every 5 s);
- watch;
- ADL (5/min when positioned);
- the desk's MCP calls;
- research.

A 429 or a -1003/418 ban blinds the stop checks, so research must never be the call that tips it.

**Rules:**
1. **Every client reads `x-mbx-used-weight-1m` on each response**, and shares it with every process on the host (`weight_ledger.py`). Scripts never call fapi.binance.com directly: they use `shortbot.http` or `shortbot.lab`, and data.binance.vision for bulk history (see "Built" below).
   - Research jobs (scan, tier 2, labelling) pause while used weight > 50% (1,200).
   - They also pause for 2 minutes after any 429.
   - The protective lane never waits.
2. **Tier 2 caps itself** at 600 weight per 15m window (`weight.TIER2_CAP`; 20 coins at the measured ~29 each; it was 150 on the ~6-8 estimate). Excess triggers queue to the next window, highest trigger score first.
3. **Streams first:** with every kline from tier 0, a deep score would be ~6-8 weight instead of ~30. Today only the 15m/1h OHLCV come from tier 0, so it is ~29 (measured, #72). Still, 20 coins per 15m is ~40 weight/min (~1.7% of the budget) against ~160/min for the 5-minute REST scan.

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
| research | yields above 50%, and for 2 min after any 429, -1003 or 418 on that host; tier 2 also has its own 600 weight per 15m cap (#72) | the scanner (scan_shorts, scan_job, tier 2 deep scores); guardian MONITOR/RESEARCH lanes (metrics, research/labelling, market_map, sweeps, supply, laggard, calendar, social, refresh); `shortbot.lab`; histdata REST tails; the backtest/refresh scripts |

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
  - MCP tools and CLI commands by the `weight` of their `commands.py` declaration, one class for both surfaces;
  - admin is protective; lab and histdata are research.
- **Writes:** any write (non-GET) makes the rest of its scope protective.
- **Shared locks:** markets reloads under `Exchanges`' shared locks always run as protective.
- **Measured limits (2026-10-02):**
  - fapi.binance.com allows **2,400**/min; demo-fapi allows **6,000**/min. Each host's own exchangeInfo sets its limit.
  - `x-mbx-used-weight-1m` arrives lowercase.
- **Ignored readings:** responses from `ticker/bookTicker` and `ticker/price`. The official SDK calls their header inaccurate.
- **Reading lifetime:** Binance doesn't document whether the minute is fixed or rolling, so a reading is trusted for 60 s.
- **Host-wide (`weight_ledger.py`, built after the 2026-10-07 ban):** Binance limits weight per IP, so every process on the host shares one budget. A raw-urllib script (472 klines limit=1000 + ticker/24hr, 8 threads, ~2,400 weight in under a minute) got the IP banned (418, -1003) for 14 min and the guardian went blind; the desk's processes had never seen its usage.
  - One SQLite file, `data/weight_ledger.db`, WAL and synchronous=OFF: it holds seconds-old state. The path is never the working directory: `SHORTBOT_WEIGHT_LEDGER` (`off` disables it); else `SHORTBOT_DB`'s directory (Docker: `/app/data`, the host's `data/` volume); else the `data/` of the checkout the code runs from, or of its main checkout when that is a git worktree (`git rev-parse --git-common-dir`). So the containers, the MCP servers and a script run with the main venv from the scratchpad, `/tmp` or `~/wt-*` all share one ledger.
  - Every desk/research response writes its reading (the freshest wins), its exchangeInfo limit and any 429/418/-1003 with Retry-After (wall times). Every desk/research request reads that state first, plus the weight other processes have in flight (reserved at admission with its up-front cost, released on the answer, ignored after 30 s), and applies the class rules above. A ban one process saw stops research and desk calls in all of them.
  - Protective calls never wait on the ledger: they never read it, and their ordinary readings stay local. Their limit answers (429/418/-1003) are shared by a background thread, never awaited.
  - Never blocking (`weight_share.py`): each ledger operation waits at most 20 ms (`weight_ledger.BUSY_MS`, the process lock and SQLite's busy timeout), 0 in the guardian (`weight_ledger.fail_fast`; `tests/loop_guard.py` checks it). On contention a write is dropped and a read falls back to the process's own readings (with nothing in flight); a dropped release is applied by the process's next ledger write, so a call is never counted twice. A corrupt file is moved aside (`weight_ledger.db.corrupt-<ts>`) and recreated. Any other ledger error is logged once.
  - **Pacing** (`weight.tagged(..., paced=True)`: `shortbot.lab`, `shortbot.http`): research weight is capped at 150 per 10 s per host (900/min, 37.5% of 2,400) across processes and threads, so a loop or a thread pool can't burst; with the ledger off or broken the same caps hold in-process.
  - **Count lanes:** `/futures/data/*` (1,000 requests/5 min/IP) and `fundingRate` + `fundingInfo` (500/5 min/IP) have their own request limits. Every desk/research call to them is counted in its lane, paced or not (the guardian included); research waits at 900 and 450 per 5 min. A 429 there cools only that lane down (desk and research wait; protective never), not the IP's weight budget; a 418 is still an IP ban.
- **Scripts never call fapi.binance.com directly** (urllib, requests, httpx, curl, ccxt clients of their own). Use `shortbot.http.get(path, params)` (research, paced, costed up front from the published weights, recorded host-wide; `lab.get` is the same) or `shortbot.lab`. Bulk history comes from data.binance.vision (`shortbot.histdata`, `histdata_vision`): a CDN, no API weight. `level_watch` reads through `shortbot.http` as desk and pauses its polls while the IP is near its limit or banned; `lifecycle_data` and the replay builders (`http.AsyncClient`) go through it as research. `tests/test_no_direct_fapi.py` fails on any fapi URL in code outside `shortbot/http.py`.
- **Published weights** (`http.cost`, https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api, read 2026-10-07): klines and its variants by limit [1,100) 1, [100,500) 2, [500,1000] 5, >1000 10 (default 500, max 1500); ticker/24hr 1 with a symbol, 40 without; premiumIndex 1 / 10; ticker/price 1 / 2; bookTicker 2 / 5; depth 2/5/10/20 for limit ≤50/100/500/1000; aggTrades 20; historicalTrades 200; exchangeInfo and openInterest 1; `/futures/data/*` 0 (own request limit). The IP limit is REQUEST_WEIGHT 2,400/min (exchangeInfo `rateLimits`).

## Build (who)
1. **Stream collector + local candle store** (`market/streams.py`): tickers, mark/premium/funding, klines for the liquid universe, a reconnect at 24h, SQLite. It is a guardian process like `LiquidationRecorder` (wired in #72).
2. **Trigger rules** (`scan_triggers.py`, pure and tested), each logged with its own counts for later evaluation.
3. **DataHub reads candles from the local store** when fresh, falling back to REST.
4. **The guardian research lane** runs triggers at each 15m close plus on events, deep-scores the triggered coins and alerts through scan_job.

## Stream limits (Binance docs, websocket-market-streams/Connect, read 2026-10-02)
- a connection carries at most 1024 streams and is cut at 24 h;
- the server pings every 3 min and drops a connection with no pong within 10 min;
- clients may send 10 messages/s;
- "IPs that are repeatedly disconnected may be banned": reconnect with backoff.

`market/streams.py` uses ≤ 200 streams per connection (2 sockets for ~183 coins × 2 timeframes), reconnects at 23 h, and buffers the ~1,400 kline msg/s into one SQLite commit per second.

## Measured trigger rates (tier 1)
Replay of the move and volume rules (`scan_triggers.py`) on real 15m bars from `data/hist5m.db`: 103 coins with ≥ 10 days of 5m bars, 5,734 windows (59.7 days).

| rule | coins/window mean | median | p90 | p99 | max |
|---|---|---|---|---|---|
| 1h move \|z\| ≥ 2 | 1.5 | 1 | 3 | 9 | 21 |
| 4h move \|z\| ≥ 2 | 2.2 | 2 | 5 | 10 | 17 |
| volume z ≥ 2 | 4.7 | 4 | 8 | 13 | 25 |
| any of the three | 5.9 | 6 | 10 | 16 | 26 |

- Scaled to the ~183-coin universe: ~10 coins per window, ~18 at p90, ~28 at p99. A cap of 20 deep scores per window (~600 weight at the measured ~29 per coin) is binding only in the busiest ~5% of windows; the excess queues by strength.
- Fat tails make |z| ≥ 2 rarer than the Gaussian 4.6% (1.5% for 1h moves): σ is inflated by the outliers.
- **Persistence:** 84% of 4h-move triggers and 60% of 1h-move triggers also fired the window before. `select(recent=...)` therefore skips a coin re-triggering with no new rule within RESCORE_S (1 h).
- Funding/premium deciles, liquidation bursts and the lower-high rule aren't replayed yet (no stored 15m premium history); their counts get logged live from day one.

## Tier-2 cost, measured (#72, 2026-10-02)
The estimate above (6-8 weight per coin) assumed every kline came from streams. ccxt's own cost model (`calculate_rate_limiter_cost`, the guard's cost per call) over the calls one deep score makes gives **~29 weight per coin**:

| input | call | weight |
|---|---|---|
| order book | depth 500 | 10 |
| 1d candles (lifecycle, controller) | klines 200 | 2 |
| 5m flow (tape) | klines 288 | 2 |
| 5m mark (robust) | markPriceKlines 288 | 2 |
| 4h candles, 1m and 1h flow (×2), 1m mark, premium | klines / markPriceKlines / premiumIndexKlines, < 100 bars | 1 each (~6; the 1m pair only after a sweep) |
| OI history ×2, funding history ×2, taker ratio, 2 long/short ratios | futures/data, fundingRate | 1 each (~7) |
| funding tables | premiumIndex + fundingInfo, once per batch | ~2 |
| 1h candles | from the stream store (REST: 1) | 0 |

The store serves only `ctx.candles` for 15m and 1h: its reader returns OHLCV, not the taker-buy volume the flow candles need, and 5m/1m, mark, premium, 4h and 1d aren't streamed. `TIER2_CAP` is therefore 600 (20 coins). The tiered scan measures the real spend per window (`scan_windows.tier2_weight`) and plans the next batch with its EWMA. Re-set the cap from those rows.

## Timeframes (2026-10-03)
**What the code reads**, from a full inventory of signals, setups, guardian jobs, the scanner and research:

| timeframe | live readers | depth |
|---|---|---|
| 1m | close quality and painted closes (every deep score), sweep acceptance, BTC/ETH 15-min impulse (market pause), warm-up | 60 (16 for BTC/ETH) |
| 5m | taker imbalance, volume heat, mark-gap, volatility (σ), squeeze watch on open positions, scan labels | 288 (labels 864) |
| 15m | sweep / CVD signals, tier-1 triggers, snapshot | 96-120 |
| 30m | none live: only the untested intraday_tsmom card (first 30 min) | — |
| 1h | core stretch / structure (critical input), pump, perp-vs-spot, market map (every coin ≥ $10M, every 10 min) | 60-170 |
| 4h | hype fade, snapshot, BTC regime | 18-60 |
| 1d | lifecycle, controller, BTC risk-budget regime, long shadow | 200-400 |

**What the evidence says about resolution:**
- **Speed matters for pump fades at the minute scale.** In the live-pipeline replay, 725 episodes entered 5-15 min late lost another 0.02-0.03 R (paired t ≈ −3; BACKTEST.md replay table).
- **The trailing verdict is the same on 5m and 1h bars** (≤ 0.008 R apart; ROUND4.md §4).
- **Close-only checks miss touches:** 23% of them on 15m closes and 38% on 1h closes (PROBABILITY.md).
- **Hourly long triggers fail; daily ones work** (LONG_PLAYBOOK.md).

**What is streamed (market/streams.py):**
- **Streamed:** 1m, 5m, 15m, 1h, 4h and 1d for the liquid universe. ~182 coins × 6 = ~1,100 streams on 6 sockets, ~4,200 msg/s. At 3.2 µs per message that is ~13 ms of CPU per second, on the collector's thread.
- **30m is built from 5m on read.** 1m bars aggregate exactly to Binance's own 5m / 15m / 30m / 1h: 84 of 84 bars identical, taker-buy volume included (BTC, SOL, BAT, 2026-10-02).
- **Taker-buy base volume is stored**, so `ctx.flow_candles` (1m, 5m, 15m, 1h) reads the store too. Backfills use raw `/fapi/v1/klines` for the same reason.
- **Backfill depth per timeframe** covers the deepest live reader: 1m 99, 5m 300, 15m 130, 1h 200, 4h 99, 1d 400. That is weight 1-2 per call, ~1,800 weight for a cold start, yielding to protective calls.

**Still REST:**
- order book (10);
- mark klines (5m × 288: 2; 1m after a sweep: 1);
- premium 1h (1);
- OI and ratio history (~7);
- funding tables (~2);
- spot 1h on the spot host.

Mark and premium klines have no kline stream. Building them from `!markPrice@arr@1s` is possible but unverified against Binance's own mark klines.

**Expected tier-2 cost:** ~20 weight per coin, down from ~29. The 1d, 4h, 5m / 1m / 15m / 1h flow and 1h candle reads move to the store. Confirm from `scan_windows.tier2_weight` after the restart, then re-set `TIER2_CAP`.

## Cadence: how often tier 1 runs (task #40, 2026-10-03)
`backtest/cadence_study.py` evaluated the bar rules every 1, 5 and 15 minutes, on 15m windows aligned to the evaluation minute. Data: the replay's 1m perp bars, 180 coins, 93 days.

An episode is a (coin, rule) trigger after 60 quiet minutes. Lead is how much earlier than the 15-minute cadence it fires. A transient is an episode no 15-minute evaluation sees within 60 minutes: a spike that reverts first.

| rule | episodes/day 1m / 5m / 15m | median lead 1m / 5m | transients 1m / 5m |
|---|---|---|---|
| move_1h | 554 / 470 / 370 | 8 / 5 min | **40% / 25.1%** |
| move_4h | 238 / 206 / 171 | 9 / 5 min | 36% / 22% |
| volume_surge | 436 / 420 / 397 | 8 / 5 min | 14% / 9% |
| lower_high_after_pump | 122 / 113 / 100 | 7 / 5 min | 20% / 13% |

New episodes per 15 minutes (the tier-2 load):

| cadence | mean | p95 | p99 |
|---|---|---|---|
| 1m | 14.1 | 32 | 71 |
| 5m | 12.6 | 29 | 67 |
| 15m | 10.8 | 25 | 59 |

**Pre-registered decision: keep 15 minutes.**
- The rule: 1m if the median lead ≥ 5 min, every rule's transients ≤ 25% and the p95 load ≤ 30; else 5m with a lead ≥ 3 min; else 15m.
- 1m fails on transients (40%) and load (32).
- 5m misses by a hair: move_1h transients are 25.1%.
- The fast events (liquidation bursts, premium spikes, the BTC/ETH impulse) still run every 15 s.

**Post hoc:**
- The price-move rules are the noisy ones.
- volume_surge and lower_high_after_pump pass every threshold even at 1m: a 7-8 min lead with 14-20% transients. lower_high_after_pump is the pump-fade trigger, the setup whose entries lose 0.022 R per 5 minutes of delay.
- Choosing per rule after seeing the pooled result would be a researcher degree of freedom. So the per-rule cadence is a hypothesis.
- It is being checked forward: the two rules evaluated every minute on live data, logged, not acted on, and judged on the same thresholds after 2 weeks.

## Tier-2 order book: depth stays at limit 500 (measured 2026-10-02)
The depth signal (`signals/depth.py`) sums the book within ±1% of mid. A 500-level book costs 10 weight; 100 levels cost 5. Of the ~29 weight a deep score costs (binance-bot-99's #72 measurement), 10 is the book.
- Live books for 57 coins, limit 500 against its first 100 levels: the first 100 levels miss part of the ±1% band on **10 of the 15 top-volume coins**, capturing BTC 13%, ETH 12%, HYPE 7%, ENA 32%.
- They also miss on 6 of 15 coins ranked 60-75, 2 of 15 ranked 150-165, and **4 of today's 15 biggest movers** (MOVR 21%, ZRO 33%, MAGMA 51%).
- The movers are what tier 2 scores, so limit 100 would bias the depth feature research_report tests against squeezes.
- A fetch-100-then-500 fallback would save about 2 weight per coin (~8%). Not worth the code at ~1.7% of the budget.

## Wired (#72)
- **Tier 0 in the guardian** (`stream_task.py`):
  - **Isolation:** `StreamTask` is a supervised task like `LiquidationRecorder`, but the collector runs on its own thread and event loop. Its synchronous work (~1,400 kline msg/s parsed, a commit a second, a SQLite busy-wait of up to 15 s) can't stall the guardian's lanes. A crash is alerted and restarted; a dropped socket is the collector's own backoff.
  - **Universe:** coin perps with 24h quote volume ≥ $5M, refreshed every 6 h. The collector restarts only when the set changed.
  - **Backfill:** `ex.public.fetch_ohlcv`, tagged research, run on the guardian's loop (ccxt's client is bound to it).
  - **Store:** `data/streams.db` beside the journal. Housekeeping prunes it daily (`KEEP_BARS`).
- **DataHub** (`market/hub.py`):
  - `ctx.candles` for 15m and 1h reads the store when it is fresh, else REST. Fresh means complete, gap-free bars **and** the coin's kline stream updated within `STREAM_MAX_AGE_S` of the caller's weight class: 90 s for research and tier 2, 15 s for desk and protective (pre-trade re-checks). A socket that went quiet leaves complete-looking bars with a stopped forming bar.
  - The rows are identical in shape. `features.candle_source` = `{tf: "stream" | "rest" | "mixed"}` sits next to `features.scan`, and `features.data_deferred` lists inputs the weight guard held back.
- **Readers are bounded by now** (the market clock, #29): no candle, latest or fresh row written after it is ever returned.
- **Tiered scan** (`scan_tiered.py`; the guardian's `scan` lane, research class, every 15 s; its own lane because the research lane's studies can hold it for 30 min):
  - **When it evaluates:** each 15m close (5 s grace) runs every rule; between closes only `fast_events` run, each (coin, rule) once per window. A BTC/ETH impulse re-checks everything.
  - **Selection:** `select` caps at 20. Tier 2 scores what fits `tier2_room()` at the measured weight per coin; the rest queue for the next close, and so does a coin starved by the weight guard (`data_deferred`), which is never logged.
  - **Logging:** tier-2 rows are research candidates with `features.scan = {"tier": 2, "wake", "rules", "strength", "carried"}`; the replay (#29) skips them. Every close is a `scan_windows` row (counts per rule, selected, scored, queued, skipped, tier-2 weight), the data for evaluating the triggers.
  - **Alerts:** tier 3 alerts are `scan_job.pick_alerts`, deduplicated in kv (`scan_alert_seen:`), so a restart doesn't re-alert within 2 h.
  - **Stale streams:** tier 1 pauses and a `streams_stale` alert goes out after 10 min. There is no REST fallback scan.

## Multi-timeframe views (task #41, 2026-10-03)
- Every streamed coin's 5m / 15m / 1h / 4h / 1d state (`mtf.py`) is computed from the store at each 15m close and logged (`mtf_live.py`, `data/research/mtf.db`), with zero exchange calls.
- `python -m shortbot.mtf_live COIN` prints one coin's view.
- The same `mtf.series` code backs `backtest/chain_study.py`, so a chained rule means the same thing in research and live.
- The first 9 chains: none is wired yet (BACKTEST.md "Chained multi-timeframe rules").

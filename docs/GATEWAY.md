# Order gateway (task #36, design, 2026-10-02)
One process owns every exchange write. The MCP servers, the CLI and the guardian submit **intents** to it. It runs them one at a time per symbol, journals each one before acting, resolves unknown outcomes by client id, and answers with a typed result. It replaces the per-symbol flock (#21) as the serialiser, works when the desk runs on AWS Tokyo, gives `Trader` a public interface, and makes one command registry the source of both the MCP tools and the CLI.

Status: design only, nothing built. Surveyed on master e45da38 (768181f changes no order path). Sources at the end, checked 2026-10-02.

## Recommendation in one screen
- **Where:** its own small process, `shortbot-gateway`, on the desk host (the host with the journal and the guardian). The guardian becomes its first client. Not inside the guardian, and not a library with a leader lock (§2).
- **Transport:** a Unix socket in a 0700 `data/run/`, one JSON line per request. The gateway journals every intent on receipt, so durability and idempotency come from the journal and liveness comes from the socket (§3). Cross-host access goes through SSH socket forwarding; no port is opened.
- **Intents** are idempotent or carry preconditions (`expect_*`). Each is keyed by an intent id the caller picks; every order inside it keeps its #9 client id (§4).
- **Fail closed** for anything that adds risk. Risk-reducing actions on the desk host fall back to a journalled break-glass write under the flock, so the flock stays (§3.4).
- **Policy stays with the caller, execution moves to the gateway.** Today's seam is `async with symbol_lock(...)`: what runs before it is policy, what runs inside it is execution (§4.1).
- **9 steps, about 3.2k lines including tests.** Each one keeps the desk working (§8). **7 owner decisions** (§9).

## 1. Current state
### 1.1 Who writes
- **guardian** (`shortbot-guardian`): one long-lived process, restarted after every merge (DEPLOY.md §5).
- **each MCP server** (`shortbot-mcp`): one per Claude Code session. Several can run at once, they live for days, and they can be stale (#68).
- **each CLI call**: a fresh process per call. The write commands are `open`, `close`, `move-stop`, `widen-stop`, `add`, `propose`, `cancel-group`, `amend`, `update-plan`, `flatten`, `kill-switch STATE` and `ladder SYM --apply`.
- **not writers:** `shortbot-admin` (it only reads equity) and the backup job.

Each process has its own ccxt client (`exchange.py:104-150`) and its own weight-guard state ("the state is per process", SCANNING.md).

### 1.2 Raw exchange writes: 20 sites in 10 files
| group | sites |
|---|---|
| open risk (3) | `trading.py:346` market SELL (`_market_sell`: open, add) · `entries_place.py:229` GTD limit SELL · `entries_amend.py:59` `edit_order` |
| stops (3 creates) | `trading.py:262` closePosition STOP_MARKET (`_place_stop`) · `stops.py:96` reduce-only bridge · `entries_place.py:270` reduce-only pre-arm |
| stop cancels (2) | `stops.py:80` (`_cancel`: old stop, bridges, strip) · `trading_manage.py:133` (`_strip_stops` once flat) |
| closes (1) | `trading.py:304` reduce-only market BUY (`_market_close`). Used by close, partial, the widen cut, forced close, close-past-stop, crossed-on-move, aborted entry and flatten |
| targets (2) | `targets_sync.py:161` reduce-only GTC BUY limit · `targets_sync.py:134` cancel |
| entry and pre-arm cancels (6) | `entries_cancel.py:38` limit, `:52` pre-arm · `entries.py:199` forgotten entry's pre-arm, `:281` pre-arm once the stop rests · `entries_fills.py:280` round trip · `entry_intents.py:184` dead entry's pre-arm |
| account config (3) | `trading.py:206` `set_position_mode`, `:209` `set_margin_mode`, `:218` `set_leverage` (all in `_prepare`). The lock audit's `_WRITES` (tests/conftest.py:42) misses these three |

### 1.3 The flows that reach them
About 30 flows reach them, through 25 `symbol_lock` acquisitions in 13 files.

**Session commands (MCP tool / CLI):**
- `open_short`/`open`: `trading_open.py:68-166`, lock at :141.
- `close_position`/`close`: `trading_manage.py:36-90`, lock at :44.
- `move_stop`: lock at :202.
- `widen_stop`: `positioning.py:139`, lock at :144.
- `add_to_winner`: lock at :218.
- `update_plan`: `mcp_trade.py:120-141`, then `targets_sync.after`.
- `propose_entries`: `entries_place.py`, lock at :203.
- `amend_entry`: lock at :52.
- `cancel_entry_group`: `entries_cancel.py:93`, then `_cancel_entry` with its lock at :67.
- `halt_trading` and `flatten_all`: `killswitch_ops.py:37-77`.
- `ladder --apply`: `ladder_apply.py:110-114`.
- Every session's opening gate can also halt on a stale heartbeat and withdraw the entries itself (`killswitch.py:214-234`).

**Guardian:**
- Positions pass (60 s):
  - `ensure_stop` (`trading_manage.py:232`, lock at :239);
  - forced close (`guardian_positions.py:226-230`);
  - the plan executor (`guardian_plan.py:42-82`);
  - `targets_sync.settle_flat` (:52).
- `cap_stop` (`guardian_margin.py:117`).
- Fill poll (5 s):
  - `_on_fill` (`entries.py:239-267`): withdraw the remainder, protect, plan stop, journal, targets;
  - expiry and cancel rules, `_forgotten` (:197);
  - OCO sibling cancels (`entries_fills.py:142-254`);
  - the governor/pause sweep (`guardian_positions.py:262`);
  - the HALTED sweep (:307-313).
- `entry_reconcile` (60 s): `entry_intents.py:125/139/183/275`.
- `tranches.cancel_legs` (:80) on a full close.

**Private `Trader` methods called from 10 other modules (18 call sites).** This is the debt behind a public interface:
- `_place_stop`: stops.py:137,163,203 and entries.py:276.
- `_market_close`: positioning.py:188, stops.py:249 and guardian_positions.py:230.
- `_market_sell`: positioning.py:262.
- `_prepare`: entries_place.py:211.
- `_raw_positions`: guardian_watch.py:42, guardian_positions.py:51, entry_intents.py:237, killswitch_ops.py:59,69 and reconcile.py:112.
- `_verified_position`: entries.py:253.
- `_find_stop`: stops.py:108.
- `_alert`: assigned at guardian.py:74.

Eight non-Trader modules also write through `tr.t` directly. LONG_BUILD.md counts about 60 order, stop, close and flatten sites among its ~150 short-only assumptions, all on these paths.

**MCP/CLI duplication.** There are 47 MCP tools and 123 argparse arguments. Six parallel name tables must agree:
- `mcp_server.UNLOGGED` and `PROTECTIVE_TOOLS`/`RESEARCH_TOOLS` (:39-49);
- `cli.READS` (:25) and `PROTECTIVE_CMDS`/`RESEARCH_CMDS` (:245-247);
- `stale_guard.CLI` and `EXEMPT` (:34-39).

`cli.tool_body` (:229) reuses some tool bodies; the rest call `Trader` directly with re-declared arguments. The two lists already differ: the CLI tags `set-params` protective; MCP tags `flatten_all` and `halt_trading` protective.

### 1.4 How the flock, the #9 client ids and the #13 write-ahead intents work together
Each mechanism covers one part:

- **flock** (`symlock.py`) makes "read the state → decide → send → journal" atomic per symbol across the processes on one host:
  - an asyncio lock sits in front of it;
  - it is reentrant per task, and a task never holds two symbols;
  - after the wait, the caller gets `SymbolBusy` and nothing is sent. Sessions wait 60 s, the guardian 5 s;
  - lock order is `Trader.lock` → symbol lock → SQLite autocommit writes.
- **Client ids** (`clientid.py`) make one send's outcome knowable:
  - every send carries `sb-<kind><ref>-<nonce>`, journalled before it goes out;
  - an ambiguous answer is resolved by looking the id up, never by resending. A Binance id is unique among open orders only: it is a lookup key, not a dedup key.
- **Write-ahead rows** make a crash mid-sequence recoverable by another process:
  - the rows are entries `placing`, trades `opening` and target rows;
  - `entry_intents.reconcile` (60 s, plus the 5 s poll for `unknown` rows), `reconcile._opening` (120 s grace), `targets_sync._settle` (90 s grace) and the make-before-break settle in `stops.py` resolve them;
  - every transition is compare-and-set (`EntryDesk._settle`).

**Gaps that remain:**
1. **No exclusion across hosts or VM mounts.** symlock.py:20-21 says so, and so does DEPLOY §4.
2. **A suspended process keeps its flock.** If the laptop sleeps mid-sequence:
   - the guardian gets `SymbolBusy` on that symbol every pass, so no pass verifies, and HALTED follows after 10 min;
   - the session wakes up mid-sequence, acting on hours-old decisions.
3. **Market sends of unknown outcome outside entries have no resolver.** These are the `close_unknown` and `add` UNKNOWN decisions; a human re-reads the position.
4. **Portfolio caps are checked per process.** `Trader.lock` serialises opens only within one process, and the flock is per symbol. Two sessions opening two different symbols can each pass the max-positions, total-notional and daily-room checks.
5. **Account-level limits are invisible to any one process.** Order rate limits are counted per account (Binance General Info), weight per IP, and each process sees only its own responses.
6. **Every writer runs its own copy of the order path.** #68 stops a stale MCP server from writing, but the guardian between a merge and its restart, or a stale lazily-imported module, still writes.

## 2. Where the gateway lives
| | A: inside the guardian | **B: own process on the desk host (recommended)** | C: library + leader lock |
|---|---|---|---|
| crash isolation | shares a process with streams (#72), research lanes and the refresh subprocess. An OOM on a t4g.small or a C-extension fault stops all writes | small process with order-path modules only. A guardian crash doesn't stop session closes, and the reverse | every process links the full order path |
| latency | in-process | one local socket hop, < 1 ms, against 5-300 ms to Binance | in-process for the leader |
| guardian on AWS | same as B | sessions reach it on the host or over SSH (§3.1) | needs a lock every host can see. flock is same-host, and SQLite WAL is same-host ("All processes using a database must be on the same host computer"). That means new infra (redis/etcd/DynamoDB lease) for 1-3 processes |
| laptop sleep | same as B | on AWS, a sleeping laptop never freezes a sequence: the gateway finishes it and the client reads the result by id | failover mid-sequence is the hard case |
| restarts after merges | every merge restarts it: 10-30 s of refused session writes, several times a day | only when its own import closure changes, drained first (§3.5) | n/a |
| ops cost | none | one more supervised process: launchd `KeepAlive` on the Mac, a second compose service from the same image on EC2 | high |
| migration | smallest: guardian flows stay in-process | guardian flows become intents (step 6) | largest |

**What the references do.** All three keep a single writer inside one process, and their operator surfaces (Telegram, REST, UI) talk to it:
- **NautilusTrader:**
  - one node per process with a single-threaded kernel ("Running multiple `LiveNode`... concurrently in the same process is not supported");
  - commands flow Strategy → RiskEngine → ExecutionEngine → ExecutionClient as point-to-point messages, and only the ExecutionClient talks to the venue;
  - its external (Redis) bus is documented for data egress, not for command ingress.
- **Freqtrade:**
  - one process per bot and per database;
  - Telegram and the REST API run inside it and take the same lock as the main loop: "Protect from collisions with force_exit. Without this, freqtrade may try to recreate stoploss_on_exchange orders while exiting is in process, since telegram messages arrive in an different thread." That is our STX incident (#21) in their words;
  - the API listens on 127.0.0.1, and they "strongly recommend to not expose this API to the internet".
- **Hummingbot:**
  - the connector is the only writer, and its `ClientOrderTracker` starts tracking a client id before the send;
  - Hummingbot Gateway (its DEX middleware) is a precedent for a separate API-server process on `localhost:15888`.

B is the same pattern, with the guardian as one more surface. A and B share steps 0-5; the choice only bites at step 6. A is the fallback if the owner prefers one process (D1).

## 3. Transport
### 3.1 Options
| | **Unix socket (recommended)** | SQLite intent queue | HTTP over TCP + TLS |
|---|---|---|---|
| latency | sub-ms (`asyncio.start_unix_server`) | polling, 100-250 ms or a wake signal; callers poll for results | ms |
| liveness | a refused connect means down, at once | a dead gateway looks like a slow one; needs a heartbeat row | as socket |
| cross-host | `ssh -L local.sock:remote.sock` (OpenSSH ≥ 6.7: "both ends may be a Unix domain socket"); no port | impossible: WAL is same-host | needs an open port. The EC2 security group is SSH-only (DEPLOY §1) |
| auth | 0700 directory + peer uid | file permissions | TLS + tokens: new secrets to manage |
| durability | the gateway journals the intent on receipt | built in, but queued intents outlive a crash and can run late on stale decisions, so they need a deadline anyway | as socket |
| contention | one writer for intent rows | every caller writes the shared journal (busy timeout 15 s, journal.py:120) | as socket |

### 3.2 Protocol
- **Socket path:** `data/run/gateway-<mode>.sock`. The mode is in the path and in every request, so a demo client can't drive a live gateway; a mismatch is refused.
- **Request:** one JSON line: `{"v":1, "op":"submit", "id":"<intent id>", "mode":"demo", "intent":{...}, "origin":{"actor":"mcp:close_position", "session":..., "pid":..., "code":"<hash>"}, "deadline_s":60}`.
- **Replies:** `accepted` (journalled, with the queue position), then one final `{"id","status","result"|"error"}`. Two more ops:
  - `{"op":"status","id":...}` returns the stored outcome;
  - `{"op":"hello"}` returns the protocol version, the code hash, the mode and the queue depths.
- **Audit:** the gateway sets `audit.acting(origin.actor)` per intent, so decision rows keep today's actor and session, plus the gateway's code version.
- **Auth:**
  - the directory carries it: unix(7) says "Portable programs should not rely on [socket permissions] for security";
  - the gateway checks the peer uid (Linux `SO_PEERCRED`, macOS `getpeereid`) against its own;
  - a socket forwarded over SSH arrives through sshd as the same user;
  - no network listener, no token on the wire.

### 3.3 Backpressure and timeouts
- **Queues:** one FIFO queue and one running sequence per symbol; symbols run in parallel, as with today's locks. A cross-symbol intent (flatten, a multi-symbol proposal, withdraw-all) is a parent that queues one step per symbol and never holds two.
- **Priority:** among queued intents (never pre-empting a running one), the protective class goes first: guardian, withdrawals, closes, flatten. This is safe because every intent re-checks its preconditions when its turn comes.
- **Admission for new risk:** risk-raising intents (Open, PlaceEntries, Add, WidenStop, a lowering amend) also pass a global admission step. It re-reads positions and resting entries and re-runs the portfolio caps, which closes gap 4.
- **Bounds:** 8 queued per symbol and 64 in total; beyond that the caller gets `busy`, nothing sent (the analogue of `SymbolBusy`).
- **Deadlines:** sessions wait 60 s (today's `LOCK_WAIT_S`); the guardian waits 5 s to start (`GUARDIAN_WAIT_S`).
  - Not started by its deadline: `expired`, nothing sent.
  - Started: it always runs to the end. `stops.set_stop`'s settle-on-cancel stays as the inner guard. A caller whose wait ends mid-run gets `pending` and asks `status(id)`.
  - A client disconnect never cancels a running intent.
- **Weight:** the gateway sees every `X-MBX-ORDER-COUNT-*` (per account) and the trade host's used weight, and publishes them to kv (the SCANNING follow-up).

### 3.4 When the gateway is down
- **What the caller sees:**
  - `GatewayUnavailable` when the connect is refused or no `accepted` arrives within 2 s: nothing was received;
  - `ReceiptUnknown` when the connection drops after the request was written. The caller asks `status(id)` after reconnecting and never resubmits under a new id.
- **Opens, adds, widens, proposals, lowering amends:** fail closed. They never fall back.
- **Risk-reducing actions and guardian protection:** on the desk host only, the same flow runs in the caller's process under the flock, as break-glass. The actions are those on `killswitch.ALLOWED` (close, partial close, tightening `move_stop`, cancelling entries, flatten) plus restore stop, cap stop, forced close and fill protection.
  - The decision rows carry actor `breakglass:<cmd>`, and an urgent alert `gateway_down_breakglass` goes out.
  - Cross-host callers can't take the flock, so they fail closed and fall back to ssh or the Binance app (D3).
- **Kill switch:**
  - tripping it stays a journal compare-and-swap in every process, so a halt works with the gateway down;
  - withdrawing the resting entries is an intent (break-glass while the gateway is down);
  - the guardian's every-cycle HALTED sweep and the gateway's start-up sweep (§5) back it up.
- **Heartbeats:**
  - the guardian heartbeat keeps its meaning (#61);
  - a new kv row `gateway_heartbeat` (every 5 s) lets the brief, `trading_state` and the opening gate say "gateway down" precisely;
  - the gate refuses new risk while that row is older than 15 s.

### 3.5 Stale code (#68) and restarts
- **Thin callers.** Callers send intents; the order path runs in the gateway. A stale MCP server's well-formed intent of a known `v` runs on current code.
  - `stale_guard` keeps refusing the policy that still runs caller-side (sizing, vetoes, budget, §4.1);
  - whether it can drop to a warning later is D5.
- **The gateway watches its own code:**
  - at start it imports its whole closure eagerly: no lazy imports in the order path, which was the #68 failure;
  - it re-hashes the modules it loaded (`procs.disk_codes`, cached 5 s, in a thread);
  - if they changed, it stops accepting, drains (≤ 30 s) and exits 0, and its supervisor restarts it on the new code. Callers see a few seconds of `unavailable`/`busy` and retry under the same id;
  - changes outside its closure (scanner, research, backtest) don't restart it. SIGTERM drains the same way.

## 4. Intent model
### 4.1 Split: policy in the caller, execution in the gateway
- **The caller** keeps everything a flow does before `async with symbol_lock`: vetoes, regime, budget, sizing, plan validation, odds. That code runs with the caller's own research parameters.
- **The gateway** runs the inside:
  - the fresh-state re-checks;
  - the kill-switch re-check (`state_refusal`);
  - journal-first, send, resolve, protect.
- **Hard caps:** the gateway re-checks them under `killswitch.effective_limits` (research override if active, else `.env`), the same limits the guardian's loss trips use.
- **Guardian-origin intents** run under the `.env` baseline, as today. A per-intent limits context, like `weight.tagged`, keeps "the AWS guardian always uses the `.env` baseline" true inside a shared process.

### 4.2 Intents
| intent | today | preconditions / idempotency | class |
|---|---|---|---|
| `Open{side, symbol, qty, stop, leverage, trade{setup, thesis, plan, features, conviction, cell}}` | inside open_short's lock + `_enter` | flat, no `opening` row, nothing resting (#12), ACTIVE, admission | raise |
| `PlaceEntries{group, legs[], ttl, prearm}` | `_place_one` + `_prearm` + `_rollback` | per leg: flat, no `opening` row; the group rolls back as a unit | raise |
| `AmendEntry{entry_id, expect_limit, new_limit}` | entries_amend.py:52-60 | compare-and-set: pending, not replaced, limit = expect | raise if lower |
| `CancelEntries{group \| ids \| all_resting, reason, keep}` | cancel_group / cancel_resting | compare-and-set per row; `withdraw` believes fills | reduce |
| `Close{symbol, fraction, of_original, expect_contracts, reason, lesson}` | `Trader.close` | `close_intent.sized`, already idempotent with `of_original` | reduce |
| `MoveStop{symbol, new_stop, reason}` | `move_stop` | tighten only; noop at the same tick | reduce |
| `WidenStop{symbol, new_stop, reason}` | `widen_stop` | once per trade; stop first, then the cut | raise |
| `Add{symbol, notional, new_stop, reason}` | `_add` | winners only, `check_add`, governor room | raise |
| `SyncTargets{symbol, why}` / `PullTargets` | targets_sync | the book against the plan: idempotent by construction | reduce |
| `Halt{state, reason}` / `Flatten{reason, lesson}` | killswitch_ops | trip (journal), then `CancelEntries(all)`, then `Close(1.0)` per position | reduce |
| guardian: `RestoreStop`, `CapStop`, `ForceClose`, `ProtectFill{entry_id}`, `ResolveEntry{entry_id}`, `CancelPrearm`, `SweepOrders` | ensure_stop, cap_stop, the forced close, `_on_fill`, `entry_intents.resolve`... | the same re-reads as today, inside the turn | reduce |

**Plan executor.** The guardian decides (`manager.plan_actions`) and sends `Close{of_original}` and `MoveStop`, both idempotent. Where several actions must see one view, it sends `PlanStep{trade_id, expect{contracts, stop}}`. Today's "decide again under the lock" (guardian_plan.py:42-56) becomes these preconditions.

### 4.3 Idempotency keys
- **The intent id** is a ULID the caller picks; it is the `intents` primary key. Re-sending an id returns the stored outcome, or attaches to the running intent; it never runs twice. Binance client ids can't do this (§1.4).
- **Who picks it:**
  - the MCP server, one per tool call. An agent retry is a new call, after a re-read, as today;
  - the CLI, one per invocation, or `--intent-id` to ask about or retry a known one;
  - the guardian, deterministically where retries should collapse: `g:<job>:<symbol>:<position fingerprint>`.
- **Orders inside an intent** keep their #9 client ids, linked in `intent_orders(intent_id, client_id, kind, symbol, sent_at, outcome)`. Any unknown order can be traced back to its intent.

### 4.4 Ordering
Per-symbol FIFO within a class (§3.3), with one running sequence per symbol. The journal records each symbol's sequences in execution order. There is no cross-symbol atomicity (there is none today either); groups roll back by compensation, as in `_rollback`.

### 4.5 Results
| status | sent? | meaning | the caller |
|---|---|---|---|
| `done` | yes | today's result dict | — |
| `refused` | no | kill switch, precondition ("changed since you looked"), risk, too small, protocol or mode mismatch | re-reads, decides again |
| `failed` | yes | a definite exchange refusal; nothing left changed (or rolled back) | reads the error |
| `unknown` | maybe | client ids unresolved. The gateway resolves them by id (≤ ~3 min) and alerts | never re-sends; `status(id)` later |
| `partial` | yes | part done: `close_incomplete`, a cut that filled short, a leg left resting | reads `remaining` |
| `expired` / `busy` | no | deadline passed before its turn / queue full | retries or drops it |
| `pending` | yes | only for a caller whose wait ended mid-run | `status(id)` |

Client-side only: `unavailable` (not received) and `receipt_unknown` (maybe received: `status(id)`).

## 5. Crash recovery and reconciliation
- **The intent journal:** an intent goes `received` → `running` → a final status. Every transition is compare-and-set, and no write transaction spans an exchange call (symlock's rule stays).
- **At start, before listening:**
  1. `received` rows from a previous run become `expired`. An intent never runs late on an old decision; the guardian re-issues its intents on its next pass.
  2. `running` rows become `interrupted`, and their `intent_orders` client ids are collected.
  3. The existing reconcilers run, in this order:
     - `targets_sync.settle_flat` and `settle` (fills first, #65);
     - `entry_intents.reconcile`;
     - `reconcile.reconcile` (`opening` rows, closed trades);
     - `RestoreStop` for every position (closePosition stop present, bridges swept);
     - the HALTED sweep, if HALTED.
  4. The interrupted intents' market sends are resolved by client id (`clientid.lookup`), with an alert per resolution. This new generic resolver also closes gap 3 for live calls.
- **The references do the same:**
  - NautilusTrader: "At startup, reconciliation aligns cached order and position state with venue reports before trader components start";
  - Freqtrade: `startup_update_open_orders`;
  - Hummingbot: `restore_tracking_states`.
- **Continuous checks:** the 5 s fill poll, the 60 s positions pass, `entry_reconcile` and the 5-min open-order sweep are the gateway's in-flight and open-order checks. In NautilusTrader that is the job of `inflight_check_interval_ms` (2 s) with `inflight_check_threshold_ms` (5 s), and of `open_check_interval_secs`.
- **The not-found rule stays stricter than theirs:**
  - NautilusTrader resolves a submit timeout to `REJECTED` after retries;
  - Hummingbot marks an order lost after `lost_order_count_limit` (3) failed lookups and keeps it fillable;
  - here, a limit not found 90 s (`GRACE_S`) after its send was never placed, and a market send is never guessed: the position decides.
- **User data stream (step 9, optional):**
  - `ORDER_TRADE_UPDATE` plus `ALGO_UPDATE` (conditional orders: NEW, CANCELED, TRIGGERING, TRIGGERED, FINISHED), consumed in the gateway, cut fill-to-stop from ≤ 5 s to sub-second, and the polls become the safety net;
  - Binance itself says a 503 means "execution status unknown": verify by WebSocket updates or by order query;
  - the listenKey needs a keepalive every 60 min.
- **Crash-only by construction:**
  - make-before-break leaves a stop at every step, and write-ahead rows leave a trail;
  - the supervisor restarts the gateway;
  - NautilusTrader takes the same stance: "abort on panic, allowing an external supervisor to restart the process".

## 6. Error vocabulary (#37)
The gateway needs these types. Each has a stable `code`, a tri-state `sent` (False / True / None = unknown), `retryable`, and `to_dict`/`from_dict` for the wire.

| family | `sent` | types |
|---|---|---|
| refused | False | `KillSwitchRefused(state)`, `PreconditionFailed(what changed)`, `RiskRefused(reasons)`, `BudgetRefused`, `Vetoed(vetoes)`, `TooSmall`, `ProtocolMismatch`, `ModeMismatch`, `StaleCode` |
| busy | False | `SymbolBusy` (exists, symlock.py:53), `QueueFull`, `Expired` |
| exchange refusal | True, nothing ran | `ExchangeRefused(code, msg)`: ccxt `ExchangeError` subclasses, `STOP_REFUSALS` (-4130/-4120/-2021), `would_trigger` |
| unknown | None | `SendUnknown(client_ids, cause)`: one type for `clientid.Unknown` and `entry_intents.SendUnknown` |
| partial / critical | True | `Incomplete(filled, remaining)`, `Unprotected(symbol, why)` (NAKED_POSITION) |
| transport | per case | `ClockDrift` (-1021), `RateLimited` (429/418/-1003), `BudgetWait` (weight.py), `Network` |
| client side | False / None | `GatewayUnavailable`, `ReceiptUnknown` |

**What #37 must provide:**
- one classifier (ccxt exception + Binance code → type) that replaces the string matching in `clientid.send_unknown`/`code`, `stops.would_trigger` and `is_clock_drift`;
- a mapping to and from today's result dicts (`ok`, `error`, `unknown`, `noop`, `rejected_by_*`), so steps 1-2 change no tool output;
- `sent` named explicitly. It is the gateway's core question.

## 7. Side-awareness (LONG_BUILD.md phase 1 → 2)
- **Every intent carries `side`** (`short` until phase 2). The gateway takes order sides from phase 1's `Side` helper:
  - the opening side, and the closing side;
  - a closePosition stop's side, and "tighter" as min or max;
  - the signed stop distance, and the side of liquidation.
- **One module of primitives.** The 20 raw sites become 9 primitives in `gateway/orders.py`, each taking a `Side`:
  - `open_market`, `place_limit`, `edit_limit`;
  - `cancel`, `place_close_stop`, `place_reduce_stop` (bridge, pre-arm);
  - `close_market`, `place_reduce_limit` (target), `prepare`.

  The ~60 order-path sites in the assumption map collapse onto them.
- **Mismatched sides are refused.** A position whose side disagrees with its journal row, or that has no row, refuses every intent except Close, Flatten and RestoreStop. That keeps today's anomaly path (REDUCING + urgent alert).
- **Signed risk sums are caller policy** (phase 1, d5). Until phase 2 the gateway refuses `side=long`. Fakes come first (CLAUDE.md).

## 8. Migration plan
Each step merges on its own with the full suite green. Behaviour changes only where a step says so.

| # | step | flock | tests | size |
|---|---|---|---|---|
| 0 | **Public `Trader`.** Public names for the 8 privates used elsewhere (`place_close_stop`, `close_market`, `open_market`, `prepare`, `raw_positions`, `verified_position`, `find_stop`, `set_alert`), callers moved; no other change. After #37 merges (same files) | as today | existing suite; a test that no module outside trading* calls a `Trader._` name | ~250 |
| 1 | **Command registry:** one `Command` per write (name, CLI name, typed args, human approval, weight class, logged, stale-exempt) generates the MCP tool and the argparse subcommand; the six tables and `stale_guard.CLI` are derived | as today | every write tool has a CLI form with the same args (extends test_cli_fallback); generated tools match today's names and metadata | ~400 |
| 2 | **Intents in-process:** intent dataclasses, `Gateway` protocol, `LocalGateway` running today's flows; MCP, CLI, guardian, killswitch_ops and ladder_apply call `submit()`; `intents` + `intent_orders` tables (write-ahead, idempotent ids) | as today | resubmitting an id runs once; each intent kind maps to today's result dict | ~500 |
| 3 | **Typed results + generic resolver** for market sends of unknown outcome (gap 3), on #37's types | as today | fakes: a close/add that times out is resolved by client id; nothing is re-sent | ~250 |
| 4 | **Gateway process + socket client**, behind `SHORTBOT_GATEWAY=off\|session` (default off). With `session`, MCP and CLI go through the gateway; the guardian still writes directly. Break-glass for risk-reducing actions; `gateway_heartbeat`; global admission (gap 4); self-restart on a stale closure; launchd plist + compose service in DEPLOY.md | the gateway also takes it per sequence, so mixed writers stay serialised | a real socket in tests: deadline/expired, busy, disconnect mid-run, receipt-unknown → `status`, mode mismatch, break-glass only for the allowed set, peer-uid refusal | ~600 |
| 5 | **Sessions on by default** after a demo soak (D6). The stale guard for writes becomes a protocol check (D5) | belt and braces | the stale-guard tests are rewritten for intents | ~100 |
| 6 | **The guardian becomes a client:** its writes are intents; startup reconciliation runs in the gateway (§5) | break-glass only | guardian suites against a gateway fixture; kill -9 mid-swap, mid-open and mid-close, then restart → reconciled and alerted | ~500 |
| 7 | **Enforcement:** `GuardedUSDM.fetch` refuses any non-GET outside the gateway's write context or a break-glass context; the lock audit adds the 3 `set_*` writes | break-glass only | a stray `create_order` from an MCP path raises in tests | ~150 |
| 8 | **Side-aware primitives** (§7); LONG_BUILD phase 2 builds here | — | the `Side` table; the short suite unchanged | ~400 |
| 9 | optional: user data stream (§5); research lanes moved out of the guardian | — | — | later |

- **Reads stay in-process everywhere.** Positions, account, stop listings, pending entries, journal, governor/budget and scans don't serialise behind the writer, which is also how NautilusTrader and Freqtrade read. Only the re-reads inside a sequence move into the gateway.
- **The flock stays for good.** Through steps 0-5 it is the belt-and-braces guard (the gateway takes it per sequence, at microseconds). From step 6 its only job is break-glass against the gateway. It is 200 lines with tests. Retiring it would only make sense if break-glass is dropped (D3).
- **Order against parallel work.** Step 0 after #37 (both touch the order-path modules); step 4 after #72 (guardian wiring); step 3 uses #55's probe results for the Query Algo Order not-found answer.

## 9. Risks, open questions, decisions
**Risks:**
- **A single point of failure for writes.** Mitigated by:
  - resting closePosition stops, which fire on Binance whatever runs;
  - break-glass on the host;
  - the supervisor and the drain.
- **Two write paths during the migration.** The flock and the lock audit cover them.
- **Policy and execution drifting apart.** Every intent carries what its caller saw (`expect_*`) and is re-checked inside its turn.
- **Long parents (flatten, groups) holding queues.** They run as per-symbol steps under the protective priority.
- **Journal contention** from the intent rows: WAL + autocommit, one writer. Measure in step 4.
- **Laptop-only topology:** everything sleeps together, as today.

**Open questions:**
- Should the order-lifecycle loops (fill poll, intent resolve, target settle, the stop invariant) move into the gateway itself, NautilusTrader-style, so protection survives a guardian crash? Revisit after step 6.
- Should the gateway re-check the hard caps, or trust the caller's policy? Recommended: re-check, under the effective limits (§4.1).
- Remote sessions would need remote journal reads, which is out of scope. DEPLOY §5 already runs Claude Code on the server.

**Decisions for the owner:**
- **D1 Where:** own process (recommended) or inside the guardian (one process fewer, restarts with every merge).
- **D2 Topology:** the desk host holds the journal, the gateway and the guardian, with sessions in tmux on it (DEPLOY §5). No laptop-to-AWS write path except over SSH.
- **D3 Break-glass when the gateway is down:** same host only, for the `killswitch.ALLOWED` set and guardian protection (recommended); never; or also cross-host.
- **D4 Longs:** LONG_BUILD phase 2 waits for step 8 (recommended; already its plan). Revisit if the gateway slips past ~6 weeks.
- **D5 Stale guard after step 5:** protocol version only for intents (recommended) or keep refusing every write from stale code.
- **D6 Soak periods** before steps 5 and 6 go default-on (suggested: 3 demo days each, with no `breakglass` or `unknown` rows left unexplained).
- **D7 Exchange user data stream** in the gateway (step 9): yes or later.

## Sources (checked 2026-10-02)
- NautilusTrader:
  - execution: https://nautilustrader.io/docs/latest/concepts/execution/
  - architecture: https://nautilustrader.io/docs/latest/concepts/architecture/
  - message bus: https://nautilustrader.io/docs/latest/concepts/message_bus/
  - reconciliation: https://nautilustrader.io/docs/latest/concepts/execution/reconciliation
  - live: https://nautilustrader.io/docs/latest/concepts/live/
  - config defaults: https://docs.nautilustrader.io/api_reference/config.html
- Freqtrade:
  - https://www.freqtrade.io/en/stable/bot-basics/
  - https://www.freqtrade.io/en/stable/rest-api/
  - https://www.freqtrade.io/en/stable/advanced-setup/
  - source: https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/freqtradebot.py (`_exit_lock`, `process`, `startup_update_open_orders`) and https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/rpc/rpc.py (`_rpc_force_exit` takes `_exit_lock`)
- Hummingbot:
  - https://github.com/hummingbot/hummingbot/blob/master/hummingbot/connector/client_order_tracker.py
  - https://github.com/hummingbot/hummingbot/blob/master/hummingbot/connector/exchange_py_base.py
  - https://hummingbot.org/gateway/
  - https://hummingbot.org/gateway/installation/
- Binance:
  - https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info (order limits per account, weight per IP, 503 unknown)
  - https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams
  - https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams/Event-Algo-Order-Update
- Platform:
  - https://www.sqlite.org/wal.html
  - https://www.openssh.org/txt/release-6.7
  - https://man7.org/linux/man-pages/man7/unix.7.html

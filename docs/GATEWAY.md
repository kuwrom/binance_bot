# Order gateway (task #36, design, 2026-10-02)
One process owns every exchange write. The MCP servers, the CLI and the guardian submit **intents** to it. It runs them one at a time per symbol, journals each one before acting, resolves unknown outcomes by client id, and answers with a typed result. It replaces the per-symbol flock (#21) as the serialiser, works when the desk runs on AWS Tokyo, gives `Trader` a public interface, and makes one command registry the source of both the MCP tools and the CLI.

Status: steps 0 (public `Trader`, §1.2), 1 (command registry), 2a and 2b (in-process intents, every write of the sessions and the guardian), 3 (typed results, the resolver), 4a (the intent sweep and the process check per pid and mount namespace, §5) and 4b/4c (the `shortbot-gateway` process, client, break-glass and heartbeat behind `SHORTBOT_GATEWAY`, default off; DEPLOY §4a) built (§8); the rest is design. Surveyed on master e45da38. Sources at the end, checked 2026-10-02.

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

### 1.2 Exchange writes: 9 `Trader` primitives (step 0), plus the listen key pair (#54)
The survey found 20 raw write sites in 10 files. Step 0 put each behind a public `Trader` primitive. Every other module writes only through these, and inside `trading*.py` only they send (`tests/test_trader_public.py`). Each resting-order primitive sends one request and returns ccxt's answer or raises its error unchanged; journalling, classification and resolution by client id stay with the caller.

| primitive (module) | sends | callers |
|---|---|---|
| `open_market` (trading_orders) | market SELL, client id, settled by id | open, add (`positioning.py`) |
| `place_limit` (trading_orders) | GTD limit SELL | `entries_place.py` |
| `edit_limit` (trading_orders) | `edit_order` on an entry limit | `entries_amend.py` |
| `place_close_stop` (trading) | closePosition STOP_MARKET, found by id | open, `_cover` (trading_manage), `stops.py`, `entries.py` (fill) |
| `place_reduce_stop` (trading_orders) | reduce-only STOP_MARKET | `stops.py` bridge, `entries_place.py` pre-arm |
| `cancel` (trading_orders) | `cancel_order`, conditional with `trigger=True` | `stops.py`, `_strip_stops`, `orphans.py`, `entries_cancel.py` (`withdraw`, `drop_prearm`), `entry_intents.py`, `targets_sync.py` |
| `close_market` (trading_orders) | reduce-only market BUY, settled by id | close, partial, the widen cut, forced close, close-past-stop, crossed-on-move, aborted entry, flatten |
| `place_reduce_limit` (trading_orders) | reduce-only GTC limit BUY (target) | `targets_sync.py` |
| `prepare` (trading) | `set_position_mode`, `set_margin_mode`, `set_leverage` | open, `entries_place.py`. The lock audit covers them since #74 (`set_position_mode` under the account lock) |
| `listen_key`, `keep_listen_key` (trading_orders) | POST / PUT `/fapi/v1/listenKey` (never DELETE: one key per account, shared) | `user_stream.py` (#54). Side-free: they stay out of step 8's `Side` table. The only guardian sends outside an intent (step 2b): the key changes no order or position, the stream must never wait for the journal (its REST calls are time-boxed), and step 9 moves the stream into the gateway. Allowlisted by name in the runtime guard (`conftest.NO_INTENT`) and in `test_trader_public.NO_INTENT` |

**Raw writes that remain: the probes only.** `probe_kit.py`, `probe_cases.py`, `probes.py` and `probe_stream.py` call ccxt directly: test orders, `countdownCancelAll` and the listen key. Two reasons keep them raw for now:
- they exist to record Binance's raw answer to each case (#55), which the desk's primitives would classify or retry away;
- they are demo-only by construction, and the lock audit already exempts them from the account lock.

Where a probe acts like the desk (a minimum short, a close), it goes through `open_market`, `close_market` and `prepare`. Step 7's enforcement must let them through: a probe context, alongside break-glass.

### 1.3 The flows that reach them
About 30 flows reach them, through 25 `symbol_lock` acquisitions in 13 files.

**Session commands (MCP tool / CLI):**
- `open_short`/`open`: `trading_open.py:68-166`, lock at :141.
- `close_position`/`close`: `trading_manage.py:36-90`, lock at :44.
- `move_stop`: lock at :202.
- `widen_stop`: `positioning.py:139`, lock at :144.
- `add_to_winner`: lock at :218.
- `update_plan`: `cmd_trade.py:146-167`, then `targets_sync.after`.
- `propose_entries`: `entries_place.py`, lock at :203.
- `amend_entry`: lock at :52.
- `cancel_entry_group`: `entries_cancel.py:93`, then `_cancel_entry` with its lock at :67.
- `halt_trading` and `flatten_all`: `killswitch_ops.py:37-77`.
- `ladder --apply`: `ladder_apply.py:110-114`.
- Every session's opening gate can also halt on a stale heartbeat and withdraw the entries itself (`killswitch.py:214-234`).

**Guardian:**
Every guardian flow below is an intent of its job since step 2b (§8); the kinds are in §4.2.
- Positions pass (60 s):
  - `ensure_stop` (`trading_manage.py:232`, lock at :239);
  - forced close (`guardian_positions.py:226-230`);
  - the plan executor (`guardian_plan.py:42-82`);
  - a flat symbol's targets (`targets_sync.flat_symbols` / `pull_flat`).
- `cap_stop` (`guardian_margin.py:117`).
- Fill poll (5 s):
  - `protect_fill` (`entries_acts.on_fill`): withdraw the remainder, protect, plan stop, journal, targets;
  - expiry and cancel rules, a forgotten entry (`entries._poll_one`);
  - OCO sibling cancels (`entries_fills.py:142-254`);
  - the governor/pause sweep and the HALTED sweep (`entry_gates.sweep`, `halt_sweep`).
- `entry_reconcile` (60 s): `entry_intents.py:125/139/183/275`.
- `tranches.cancel_legs` (:80) on a full close.

**Private `Trader` methods called from other modules: resolved by step 0.** The survey found 18 call sites in 10 modules, plus 6 in the probes. Each now has a public name: `_place_stop` → `place_close_stop`, `_market_close` → `close_market`, `_market_sell` → `open_market`, `_prepare` → `prepare`, `_raw_positions` → `raw_positions`, `_verified_position` → `verified_position`, `_find_stop` → `find_stop`, and the guardian's assignment to `_alert` → `set_alert(fn)`.

No aliases are left. `tests/test_trader_public.py` fails if a module outside `trading*.py` touches a private `Trader` member. Tests are exempt.

**MCP/CLI duplication** (fixed by step 1). There were 47 MCP tools and 123 argparse arguments, and six parallel name tables had to agree:
- `mcp_server.UNLOGGED` and `PROTECTIVE_TOOLS`/`RESEARCH_TOOLS`;
- `cli.READS` and `PROTECTIVE_CMDS`/`RESEARCH_CMDS`;
- `stale_guard.CLI` and `EXEMPT`.

`cli.tool_body` reused some tool bodies; the rest called `Trader` directly with re-declared arguments. The lists had already drifted: the CLI tagged `set-params` protective, MCP tagged `flatten_all` and `halt_trading` protective. Now `commands.py` holds one declaration per command, and both surfaces are generated from it.

### 1.4 How the flock, the #9 client ids and the #13 write-ahead intents work together
Each mechanism covers one part:

- **flock** (`symlock.py`) makes "read the state → decide → send → journal" atomic per symbol across the processes on one host:
  - an asyncio lock sits in front of it;
  - it is reentrant per task, and a task never holds two symbols;
  - after the wait, the caller gets `SymbolBusy` and nothing is sent. Sessions wait 60 s, the guardian 5 s;
  - lock order is the account lock → symbol lock → SQLite autocommit writes. Risk-adding flows (open, limit entry, add, lowered amend, widen) hold the account lock (`<lock dir>/_account.lock`, same mechanism, `AccountBusy`) from their cap checks through the send (#74); risk-reducing paths and the guardian never take it.
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
3. ~~**Market sends of unknown outcome outside entries have no resolver.**~~ Fixed by step 3: `resolver.py` settles every unknown or interrupted intent by its orders' client ids (`close_unknown`, `add` UNKNOWN, an entry's open) and alerts `intent_resolved`.
4. ~~**Portfolio caps are checked per process.**~~ Fixed by #74: the account lock serialises every risk-adding flow across the processes on this host, and the caps count resting entries and entries of unknown outcome (`exposure.py`). Same-host only, like the symbol flock.
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
| contention | one writer for intent rows | every caller writes the shared journal; on an event loop as non-waiting units (#79, journal_writer.py), off a loop with a 15 s busy timeout | as socket |

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
  - a kv row `gateway_heartbeat` (every 5 s) feeds the brief and the guardian's urgent `gateway_down` alert (older than 15 s);
  - as built (step 4), no heartbeat gate on new risk: connect plus hello is the exact liveness signal, and a 15 s heartbeat gate would refuse falsely whenever the journal's write lock delays the post.

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
- **The journal never makes an event loop wait** (#79, `journal_writer.py`). Receipt and start are one unit the caller awaits before anything runs; the result is posted. Journal-first has one exception: a protective intent whose sends are idempotent waits at most `CLAIM_WAIT_S` (2 s) while another process holds the write lock, then runs; its rows are posted and marked `journal_late`. The set is `gateway.LATE_OK`, each kind justified there: `restore_stop`, `force_close`, `cap_stop` (by client id, one closePosition stop per side), `protect_fill` (a fill's stop must never wait), `cancel_entry` and `withdraw_entries` (removing resting risk, rows by compare-and-set; the latter also for the session watchdog). Every other session, CLI and risk-adding intent waits for its row.

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
| guardian: `restore_stop`, `cap_stop`, `force_close`, `plan_step`, `pull_targets{symbol}`; entry rows (ctx = the `EntryDesk`, `entries_acts.py`): `protect_fill{entry_id, order}` (OCO siblings included), `cancel_entry{entry_id, status, reason}`, `forget_entry{entry_id}`, `withdraw_entries{reason, ids}`, `withdraw_order{entry_id, order_id}`, `rollback_split{entry_id, tranche}`, `cancel_prearm{entry_id, order_id}`, `sweep_orphans{symbol}` | ensure_stop, cap_stop, the forced close, the plan, the fill poll, the sweeps, `entry_intents.reconcile`, the orphan sweep | the same re-reads as today, inside the turn; the caller decides from reads, the handler sends | reduce |

**Plan executor.** The guardian decides (`manager.plan_actions`) and sends `Close{of_original}` and `MoveStop`, both idempotent. Where several actions must see one view, it sends `PlanStep{trade_id, expect{contracts, stop}}`. Today's "decide again under the lock" (guardian_plan.py:42-56) becomes these preconditions.

### 4.3 Idempotency keys
- **The intent id** is a ULID the caller picks; it is the `intents` primary key. Re-sending an id returns the stored outcome, or attaches to the running intent; it never runs twice. Binance client ids can't do this (§1.4).
- **Who picks it:**
  - the MCP server, one per tool call. An agent retry is a new call, after a re-read, as today;
  - the CLI, one per invocation, or `--intent-id` to ask about or retry a known one;
  - the guardian, deterministically where retries should collapse: `g:<job>:<symbol>:<position fingerprint>`.
- **Orders inside an intent** keep their #9 client ids, linked in `intent_orders(intent_id, client_id, kind, symbol, sent_at, outcome)`. Any unknown order can be traced back to its intent.
- **Built in step 2a** (`intents.py`, `gateway.py`):
  - one `Intent` dataclass. A command's kind is its MCP name (else its body's), and its args schema is the body's signature in the registry, so no per-kind class can drift from it;
  - the `intents` row replaces the write's #24 `call` decision row: it is written before the call runs, holds the request, actor and Claude session, then the status and today's result. Request columns are frozen and a final status is final (triggers), like decisions. Decision and alert rows written while an intent runs carry `intent_id`;
  - `intent_orders(client_id, intent_id, kind, symbol, at, outcome)` is written by the primitives just before each send, its `outcome` once known (step 3: their answer, or the resolver). `entries`, `target_orders` and the trade's open id keep their own state and are reached through the client id; nothing is copied;
  - statuses are §4.5's, exact by what the run sent (step 3, `results.py`): every primitive records its sends in the run's ledger (`intents.send` / `sent`);
  - guardian ids are one ULID per attempt for now: in-process there is no retry to collapse, and a fixed id would stop the next pass from retrying a failed restore. The fingerprint ids come with step 6's transport.

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
**Built in step 3** (`results.py`): `Result(status, sent, side, body, error)`. `body` is today's result dict, unchanged on every surface. `refused` means nothing was sent; `failed` means a send, then nothing left changed; `partial` means an order part-filled or the answer reports what it kept (`closed`, `cancelled`, `unprotected`, a plan step's `done`); `unknown` means a client id is still in doubt. `pending` comes with step 4. The row keeps `status`, `sent`, `result` and `error` (`errors.OrderError.to_dict`).

## 5. Crash recovery and reconciliation
- **The intent journal:** an intent goes `received` → `running` → a final status. Every transition is compare-and-set, and no write transaction spans an exchange call (symlock's rule stays).
- **In-process (steps 2a, 3):** there is no gateway start. The guardian's `entry_reconcile` job (its first tick as the guardian starts) ends the open intents of a process that is gone (pid check, one host per D2, only for rows of its own pid namespace: step 4a, DEPLOY §4) or that have been open over an hour (pid reuse, or a row from another pid namespace): received → expired, running → interrupted, alerted as `intent_interrupted` with their client ids. Then `resolver.py` settles every unknown or interrupted intent: each order without an outcome is looked up by its client id (`not_found` 90 s after its send), and the intent ends done, refused or partial, once, with an `intent_resolved` alert. It never re-sends; trade state stays with the existing reconcilers.
- **At start, before listening:**
  1. `received` rows from a previous run become `expired`. An intent never runs late on an old decision; the guardian re-issues its intents on its next pass.
  2. `running` rows become `interrupted`, and their `intent_orders` client ids are collected.
  3. The existing reconcilers run, in this order:
     - a flat symbol's targets (`targets_sync.pull_flat`) and `settle` (fills first, #65);
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
- **User data stream (#54 in the guardian; step 9 moves it into the gateway):**
  - `ORDER_TRADE_UPDATE` plus `ALGO_UPDATE` (conditional orders: NEW, CANCELED, TRIGGERING, TRIGGERED, FINISHED) cut fill-to-stop from ≤ 5 s to sub-second, and the polls become the safety net;
  - built in #54 (`user_stream.py`, `user_events.py`): an event about one of our orders runs the fill poll or the positions pass at once (`Lane.trigger`); the polls keep their schedule; health is judged by keepalive and by fills the poll finds that the stream missed, never by the connection;
  - Binance itself says a 503 means "execution status unknown": verify by WebSocket updates or by order query;
  - the listenKey needs a keepalive every 60 min.
- **Crash-only by construction:**
  - make-before-break leaves a stop at every step, and write-ahead rows leave a trail;
  - the supervisor restarts the gateway;
  - NautilusTrader takes the same stance: "abort on panic, allowing an external supervisor to restart the process".

## 6. Error vocabulary (#37)
The gateway needs these types. Each has a stable `code`, a tri-state `sent` (False / True / None = unknown), `retryable`, and `to_dict`/`from_dict` for the wire.
#37 built `errors.classify` and the types. `sent`, `code`, `to_dict`/`from_dict` (with `retryable`) and `wrap` were removed in #77 and came back in step 3, with the intent's `error` column and the result's error as their callers.

| family | `sent` | types |
|---|---|---|
| refused | False | `KillSwitchRefused(state)`, `PreconditionFailed(what changed)`, `RiskRefused(reasons)`, `BudgetRefused`, `Vetoed(vetoes)`, `TooSmall`, `ProtocolMismatch`, `ModeMismatch`, `StaleCode` |
| busy | False | `SymbolBusy` (exists, symlock.py:53), `QueueFull`, `Expired` |
| exchange refusal | True, nothing ran | `ExchangeRefused(code, msg)`: ccxt `ExchangeError` subclasses, -4130/-4120/-2021 (`errors.StopExists`, `Refused`, `WouldTrigger`) |
| unknown | None | `SendUnknown(client_ids, cause)`: one type for `clientid.Unknown` and `entry_intents.SendUnknown` |
| partial / critical | True | `Incomplete(filled, remaining)`, `Unprotected(symbol, why)` (NAKED_POSITION) |
| transport | per case | `ClockDrift` (-1021), `RateLimited` (429/418/-1003), `BudgetWait` (weight.py), `Network` |
| client side | False / None | `GatewayUnavailable`, `ReceiptUnknown` |

**What #37 must provide:**
- one classifier (ccxt exception + Binance code → type) that replaces the string matching in the order path (done: `errors.classify`);
- a mapping to and from today's result dicts (`ok`, `error`, `unknown`, `noop`, `rejected_by_*`), so no step changes tool output (done in step 3: `results.Result.of` / `from_row`), with `sent` named explicitly (the gateway's core question).

## 7. Side-awareness (LONG_BUILD.md phase 1 → 2)
- **Every intent carries `side`** (`short` until phase 2). The gateway takes order sides from phase 1's `Side` helper:
  - the opening side, and the closing side;
  - a closePosition stop's side, and "tighter" as min or max;
  - the signed stop distance, and the side of liquidation.
- **One module of primitives.** Step 0's 9 `Trader` primitives (§1.2) move to `gateway/orders.py`, each taking a `Side`:
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
| 0 | **Public `Trader`** (done). Public names for the 8 privates used elsewhere (`place_close_stop`, `close_market`, `open_market`, `prepare`, `raw_positions`, `verified_position`, `find_stop`, `set_alert`); 5 more primitives (`cancel`, `place_limit`, `edit_limit`, `place_reduce_stop`, `place_reduce_limit`) carry the raw `tr.t` writes of 8 modules; callers moved, wire and results unchanged. Probes stay raw (§1.2) | as today | existing suite; `test_trader_public.py`: no `Trader._` name outside trading*, no raw write outside the primitives and probes | ~300 |
| 1 | **Command registry** (done). One declaration per command in `commands.py`: MCP name, CLI name, args typed by the body's signature, read / write, weight class, stale-exempt, CLI hint. It generates the MCP tool and the argparse subcommand. The six tables, `stale_guard.CLI` and `cli.tool_body` are gone. Pairs whose bodies differed now share the MCP body, and the weight classes agree (`set-params`, `flatten` and `kill-switch` are protective) | as today | `test_commands.py`; `test_cli_fallback.py` (every write has a CLI form the parser accepts); the `commands` block in `surface.json`; `tools/list` unchanged | ~400 |
| 2a | **Intents in-process** (done). `Intent`, the `Gateway` protocol, `LocalGateway` running today's code; every MCP and CLI write submits one intent (killswitch_ops and ladder_apply run inside their command's); the guardian's `restore_stop`, `force_close`, `cap_stop` and `plan_step` are intents of its jobs; `intents` + `intent_orders` tables; the interrupted-intent sweep | as today | `test_intents.py`: journal first, a resubmitted id runs once, in progress, busy re-runs, status mapping, append-only rows, sweep; `test_intent_paths.py`: one intent per write on both surfaces, linked orders and decisions, a crash mid-send resolved as before, guardian actors | ~600 + tests |
| 2b | **The guardian's other writes as intents** (done). The fill poll (protect a fill with its OCO siblings and tranche joins, expiry and cancel rules, a forgotten entry), the HALTED and governor/pause/resize sweeps, `entry_intents.reconcile`'s resolutions, the orphan sweep, a flat symbol's targets, and the session watchdog's withdrawal (from a read: an intent of the reader; from a write's gate: inside that write's). Each caller decides from reads; a handler sends (`entries_acts.py`, ctx = the `EntryDesk`; `pull_targets`, ctx = the guardian). A withdrawal that keeps failing is resubmitted 5, 10, 20, 40, then 60 s apart (`entry_gates.Retry`), not every 5 s pass. The listen key is the one allowlisted exception (§1.2). After it, step 6 is a transport change only | as today | the lock audit fails any write reached from the guardian (or the watchdog) outside an intent; `test_guardian_intents*.py`: each site's kind and actor, stored requests re-bind their handlers, a quiet pass submits none, 10 failing passes ≤ 5 rows; `test_guardian_late.py`: late_ok kinds send under a held lock, a strict one journals first | ~450 + tests |
| 3 | **Typed results + generic resolver** (done). `results.py`: statuses exact by what was sent; each primitive records its sends and their outcomes (`intent_orders.outcome`); the error wire helpers are back. `resolver.py` settles unknown and interrupted intents by client id from `entry_reconcile` (gap 3). `plan_step` answers `ok: false` when an action failed | as today | `test_results.py`: every result shape round-trips; `test_intent_status.py`: refused, failed (a stop swap after its bridge), partial (a short fill; T1 then a failed breakeven swap), unknown; `test_resolver.py`: filled, not found, an add never placed, never re-sent | ~550 + tests |
| 4 | **Built (4a, 4b/4c).** `shortbot-gateway` and `RemoteGateway`, behind the boolean `SHORTBOT_GATEWAY` (default off). With it on, the MCP, CLI and web console commands declared `orders=True`, plus the watchdog's `withdraw_entries`, go through the gateway; the guardian still writes directly. Break-glass for the risk-reducing set; `gateway_heartbeat` and a guardian `gateway_down` alert; admission stays the #74 account lock. Limits: connect 2 s (retried 5 s), accepted 30 s, final 300 s (then `pending`), 8 in flight per symbol and 64 in total. Dropped as unneeded: self-restart on a stale closure (the image can't change underneath it) and per-symbol parent/child queues (bodies hold one symbol lock at a time). Compose service in DEPLOY §4a (proposed) | the gateway also takes it per sequence, so mixed writers stay serialised | a real socket in tests: busy, disconnect mid-run, receipt-unknown → `status`, version and mode mismatch, break-glass only for the allowed set, peer-uid refusal | ~600 |
| 5 | **Sessions on by default** after a demo soak (D6). The stale guard for writes becomes a protocol check (D5) | belt and braces | the stale-guard tests are rewritten for intents | ~100 |
| 6 | **The guardian becomes a client:** its writes are intents; startup reconciliation runs in the gateway (§5) | break-glass only | guardian suites against a gateway fixture; kill -9 mid-swap, mid-open and mid-close, then restart → reconciled and alerted | ~500 |
| 7 | **Enforcement:** `GuardedUSDM.fetch` refuses any non-GET outside the gateway's write context or a break-glass context; the lock audit adds the 3 `set_*` writes. It must allow POST/PUT `/fapi/v1/listenKey` from the guardian's user stream until step 9 moves the stream into the gateway (§1.2) | break-glass only | a stray `create_order` from an MCP path raises in tests | ~150 |
| 8 | **Side-aware primitives** (§7); LONG_BUILD phase 2 builds here | — | the `Side` table; the short suite unchanged | ~400 |
| 9 | optional: user data stream (§5); research lanes moved out of the guardian | — | — | later |

- **Reads stay in-process everywhere.** Positions, account, stop listings, pending entries, journal, governor/budget and scans don't serialise behind the writer, which is also how NautilusTrader and Freqtrade read. Only the re-reads inside a sequence move into the gateway.
- **The flock stays for good.** Through steps 0-5 it is the belt-and-braces guard (the gateway takes it per sequence, at microseconds). From step 6 its only job is break-glass against the gateway. It is 200 lines with tests. Retiring it would only make sense if break-glass is dropped (D3).
- **Order against parallel work.** Step 0 after #37 (both touch the order-path modules); step 4 after #72 (guardian wiring). The resolver checks a stop's not-found Query Algo Order answer against the open conditional orders (#55 hasn't verified it).

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

**Decisions (settled 2026-10-03; the owner agreed to one desk host and left the rest to practicality):**
- **D1 Where:** its own process, `shortbot-gateway`. Merges restart the guardian, so the gateway must not restart with it. Trade-off as built: a deploy (`up -d --build`) still restarts it; it drains running intents first and clients retry their connection for 5 s. Skipping restarts when its code is unchanged waits for the soak's evidence.
- **D2 Topology:** one desk host holds the journal, the gateway, the guardian and the trading sessions. The desk host is the EC2 box (eu-central-1) since 2026-10-03; journal, guardian, gateway and sessions all run there, with sessions in tmux over SSH (DEPLOY §5). The journal and the order path are never split across hosts.
- **D3 Break-glass when the gateway is down:** same host only, for the `killswitch.ALLOWED` set and guardian protection; cross-host callers fail closed.
- **D4 Longs:** LONG_BUILD phase 2 waits for step 8.
- **D5 Stale guard after step 5:** only the protocol version is checked for intents. The gateway runs the code on disk, so a stale client can't send a stale order path.
- **D6 Soak periods:** 3 demo days before steps 5 and 6 go default-on, with no `breakglass` or `unknown` rows left unexplained.
- **D7 Exchange user data stream:** now. #54 builds it in the guardian as a latency trigger, with REST polls as the truth, and step 9 moves it into the gateway.

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

# Coin memory

A rolling memory of the coins the desk is watching: what each one is doing now, measured against its own recent days, and the session's current read of it. Code: `coin_stats.py` (pure measurements), `coin_memory.py` (store, focus set, views), `coin_pass.py` (one pass), `guardian_coins.py` (the guardian lane).

## Why it looks like this
- **A coin's scale persists, its direction doesn't.** Vol (rho 0.46) and wick share (rho 0.40) carry over month to month; drift and post-rally behaviour don't (learning #54). Single-coin direction rules are noise at our sample sizes (#55). Quiet/active state is partly predictable, the same across coins (#56).
- **Tells last about a day.** In the QNT replay, Thursday's tells didn't carry into Friday (#79, #80). So a read expires: 24h by default, 72h at most.
- **The program measures; the session judges.** No LLM runs in a guardian job (#81). The pass measures, alerts and wakes a session; the session rewrites the read.
- **Alerts never exit a trade.** `break_watch` says a break looks real; manage-position decides.

## The focus set
`coin_memory.focus_symbols`, at most 12 coins, in this order: open trades, resting entries, the watchlist (`watch`), then the top 5 tradable candidates of the last hour. A coin out of focus for 3 days is archived.

## One pass (every 15 min, guardian lane `coins`, RESEARCH weight)
Per coin, through DataHub: 5m bars (3 days), 1h bars (20 days), 5m OI (1h), the latest long/short ratios, 5m taker flow (1h), funding and its history, macro events (counts only) and news (a count only, never the text). Liquidations come in one batch from the recorder. The fake-out p90 comes from the sweep study (`profile.level_behaviour`, shrunk toward all coins), recomputed at most daily in a worker thread.

The state (`coin_state.state`):
- **swings**: zigzag at 2x the mean true range; median swing size and duration, the current leg (5m over 3 days, 1h over 20 days)
- **regime**: the last closed 15m block vs the coin's trailing day. Efficiency (net move / path) ≥ 0.5 is `trend_up`/`trend_down`; otherwise `quiet` (relative vol < 1) or `active`
- **rel**: price, OI, global and top-trader L/S and funding as a z-score and a change against the coin's own 3 days. The state keeps its own 15m series of L/S, OI and price, because `long_short()` returns only the latest point
- **events** (`new` = not in the previous pass):
  - `flush`: OI −3% in 15m with price falling, or a long-liquidation burst
  - `retail_flip`: global L/S +0.05 within 60 min while price sags (#60)
  - `oi_surge`: OI +5% in 1h (the watch lane's squeeze threshold)
  - `btc_impulse`: BTC ±0.8% in 15m (listed per coin, never alerted per coin: `market_pause` covers it)
  - `level_accepted`: 2 closed 15m blocks above the prior high or round number, or below the prior low
  - `entry_near`: a resting entry within half the median swing
  - `headline`: the news count rose
- **break** (open shorts only): #81's four machine checks. Two or more true = a real break; one alone = noise.
  1. *beyond noise*: the move up from the low since entry (last 4h) exceeds the larger of the fake-out p90 and the median swing
  2. *accepted*: 2 closed 15m blocks above the prior high, or above the current read's `invalidation`
  3. *participation*: price up over the hour with OI up ≥ 1% and taker buyers ahead (imbalance ≥ +0.05). Price up with OI down is covering, not participation
  4. *source*: BTC up ≥ 0.8% in 15m: the market is lifting it

  The fifth check, new information (news, a catalyst outcome), is the session's call.

Alerts: `coin_event` (routine, once an hour per coin) for new events; `break_watch` (urgent) at a break score of 2 or more.

## Reads
`update_coin_read(symbol, read, bias, levels, invalidation, expires_h=24)` (`shortbot coin-read`). The gist is at most 1200 characters; bias is up, down or neutral; levels are named prices; invalidation is the price that proves the read wrong. `coin_reads` is append-only: a new read becomes the current one, and earlier reads stay as history. The current read is the newest one, while it hasn't expired.

**read_score.** Each expired read is scored by the move from `price_at` to the price at its expiry (the pass fetches it). An up or down read is a hit when price went its way; a neutral read reports its median absolute move. A coin flip hits about 50%, and under 30 scored reads it's an anecdote.

## Tools and the brief
- `coin_memory` (`shortbot coin-memory [COIN] [--refresh]`): one line per focus coin, or one coin in full; `refresh=True` runs the same pass now.
- `update_coin_read` (`shortbot coin-read`), `watch` (`shortbot watch COIN [--remove] [--reason ...] [--days N]`).
- The brief shows at most 8 focus coins, e.g. `QNT active vol 1.9x | swing 2.1%/40m | fakeout p90 0.9% | read 3h (down) | break 2/4 | events: retail_flip`. It never shows third-party text or the read itself. Headlines reach the agent only through the tool, as `untrusted_text`.

## Tuning
The thresholds are constants at the top of `coin_stats.py`. They are starting values: change them on evidence (alert usefulness, read_score, break-score outcomes on closed trades), not on opinion.

# Market map: many coins in parallel (research 2026-09-28)

Tags: [S] strong/replicated, [M] one solid study, [W] weak/folklore, [P] official docs/practitioner.

## Evidence
- **Three factors explain crypto returns:** market, size, momentum (Liu-Tsyvinski-Wu, JF 2022). [S] Most of an alt's daily move is BTC/market beta.
- **There is sector structure on top of the market mode:**
  - "dominant market factor and several sectoral components" in 1m data on 140 coins (arXiv 2512.06473). [M]
  - Louvain clustering finds ~15 communities (2108.09763). [M]
  - Consensus clusters are stable (2505.24831). [M]
  - In crashes, correlations jump and sectors collapse into beta (2405.05642). [M]
- **Momentum:**
  - Community momentum is claimed at 1.08%/day before costs (2108.11921). [M, no costs]
  - Cross-sectional momentum lasts ~1–4 weeks. [S]
  - Intraday sector rotation has no rigorous evidence, so treat it as context. [W]
- **Lead-lag exists, but leadership rotates:** ETH is the most influential and BTC is fading (2606.25466); CEXs lead price discovery (2506.08718). [M]
  - Lags are seconds to minutes, so this is NOT tradable at our 10-minute cadence. Use it only as a veto: don't short right after a BTC/ETH up-impulse.
- **Breadth, dispersion and correlation** come from equity practice [P/W]:
  - High breadth + BTC up = risk-on.
  - High dispersion + low correlation = coin-picking works.
  - Short laggards in weak sectors, not sector leaders. The exception is the separate pump-fade setup for extreme outliers.

## Data
- **Taxonomy:** Binance product tags from the undocumented `bapi/asset/v2/public/asset-service/product/get-products`, e.g. Layer1_Layer2, defi, AI, RWA, Meme, Gaming, Infrastructure. [P]
  - These map directly to Binance base assets. We pick ONE primary sector per coin by a fixed priority. The endpoint can change without notice.
  - CoinGecko categories are the fallback. The free Demo key allows 100 calls/min and 10k calls/month, and requires attribution.
- **Prices:** REST 1h klines for the liquid universe, one pass per map.
  - WebSocket `!miniTicker@arr` is on `wss://fstream.binance.com/market/ws/` (routed paths since 2026-03-05, 1024 streams per connection, 24h lifetime) if we ever need it live.

## Design (thresholds are starting assumptions, logged for testing)
- **Residual move:**
  - β = rolling OLS of 1h log returns on BTC over 7 days, shrunk 30% toward 1 and clipped to [0, 3].
  - resid_BTC = r − β·r_BTC over 1h, 4h and 24h.
  - resid_sector = resid_BTC − the sector median. Also given as a z-score against the coin's residual volatility.
- **Header:** BTC and ETH returns (1h/4h), breadth (% of coins above their 1h EMA20), the 1h advance/decline ratio, dispersion (MAD of 1h returns), and a correlation proxy (var of the equal-weight index ÷ mean single-coin var).
  - RISK-ON when breadth > 65% and BTC 4h > 0. RISK-OFF when breadth < 35%.
- **Sector rows, ranked by median 4h resid_BTC:**
  - median returns (1h/4h/24h), breadth
  - leader and laggard (by 24h resid_sector)
  - volume surge (1h vs the 7-day hourly average; flagged at 2×)
  - funding tilt vs the market.
- **Rotation-in:** the sector's 4h residual rank rises from the bottom half into the top 3, with breadth > 60% and volume surge > 1.5×, for 2 maps in a row. Rotation-out is the mirror image and marks where to hunt shorts.
- **Hard vetoes for new shorts** (`veto_override` needs a reason):
  1. The sector is rallying: median 4h > +3% and breadth > 70%.
  2. The sector is rotating in, or the coin is its sector's leader.
  3. Market squeeze: breadth > 75% and BTC 1h > +1.5%.
  4. BTC or ETH up more than 0.8% in 15 min.
- **Context:** preferred shorts are laggards (resid_sector z < −1) in sectors with negative 24h relative strength.

## Level sweeps: measured on our own data (`shortbot sweeps report`, guardian extends it daily)
- **Evidence:**
  - Osler 2003: stops cluster just past round numbers and take-profits sit AT them.
  - Osler 2005: crossings more often continue.
  - Osler 2000: support/resistance levels bounce 60.8% of the time vs 56.2% for random levels (thin edge).
  - No published crypto overshoot distribution exists, so we measure our own.
- **First run** (2026-09-28: 60 perps, 3 days, 3,212 events, a falling market, so regime-specific):

  | Level type | Failed within 4h | p90 fake-out (mark) |
  |---|---|---|
  | Round tier 1–2 | 82% | 4.1% / 1.4 ATR |
  | Round tier 3 | 78% | 5.2% / 1.8 ATR |
  | Prior 24h high | 88% | 2.5% / 1.25 ATR |
  | Random non-round level (control) | 82% | 2.2% |

- **Reading:** round numbers aren't better reversal points, but they are run further (stop fuel). The 24h high is the best short level.
- **Stop rule** (the `sweep_proof` stop in `snapshot`):
  - Take the nearest obvious level above price, plus the larger of its p90 fake-out % and p90 ATR multiple.
  - Keep the stop clear of every round number: not within 0.3% above one or 0.2% below one.
  - Size = risk ÷ that distance.
- **Method:**
  - Event = the first 1m bar above a level after ≥ 60 min below it, with a 240-min cooldown per level.
  - Fake-out = the max excursion until a close below L − 0.25 ATR(1h), measured on last and mark.
  - Trust p90 with n ≥ 300 and p95 with n ≥ 500. Events co-move with BTC, so they are not independent.

## Market news: unscheduled market-wide headlines (shadow, #92)
- **Why:** on 2026-10-08 every sector fell 3.6-4.8% in 4h on BTC-level headlines that had sat in `coin_news BTC` for
  hours (US government moving $770M then $1B of seized BTC, record ETF outflows, US-Iran strike fears). The calendar
  covers scheduled macro, the map's pause covers BTC impulses and `news_on_position` covers our own coins; nothing
  covered unscheduled market-wide news.
- **Job:** the market guardian's research lane, every 5 min (`market_news.py`). It reads BTC and ETH headlines from the
  existing sources (`news.news_for`: Binance announcements and the RSS feeds, cleaned as untrusted text) and tags titles
  with a keyword classifier: `holder_transfer` (government, whale, miner or treasury moving or selling coins),
  `etf_flow`, `hack_insolvency` (a hack naming a venue or $50M+, or withdrawals halted), `geopolitics` (war, strikes,
  tariffs) and `regulation` (an agency plus an enforcement or approval verb). Deduped by link and by normalised title;
  items older than 3h and everything present at the first run are marked seen, never alerted.
- **Alert:** one `market_news` alert per new tagged item, urgent, to both desks (`alerts_view.MARKET_ACTORS`). After 3 in
  an hour, the rest of the burst arrive as routine `market_news_more`. The first line is ours (classes, coin, source,
  time); the headline is third-party text: read it with `coin_news BTC` / `coin_news ETH`.
- **Research log:** `market_news_events` keeps each item with BTC, ETH and the 20 most liquid alts' prices and the map's
  header at detection. The hourly research job labels it after 24h: BTC/ETH returns at +1h/+4h/+24h and their lowest
  low within 4h/24h, and the alt basket's median return and share of alts down. `shortbot study market_news` reports by
  class, one mean per UTC day (one story's headlines cluster).
- **Status:** context, not a trigger. It changes no gate, size or order. Before it becomes an entry gate it needs a
  registered test against the unconditional returns of the same weeks, on enough distinct days.

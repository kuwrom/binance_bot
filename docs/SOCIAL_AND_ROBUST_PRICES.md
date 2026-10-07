# Social signals, engineered closes and signal decay (research 2026-09-28)

## 1. Engineered closes are real, so trust time and volume over the print
**Evidence:**
- Marking the close is documented in equities: quarter-end last-minute buying inflates prices, which then reverse (Carhart et al. JF 2002).
- Close manipulation can be detected from the return at the close, the next-period reversal, and volume at the close (Comerton-Forde & Putniņš JFI 2011). It is concentrated in illiquid names (Chau & Foley).
- In crypto, over 70% of volume on unregulated venues is wash trading (Cong et al. MS 2023).
- **Binance itself doesn't trust the last price.** Mark = median(index-based estimates, last), and the index caps outlier venues. CME's BRR uses a volume-weighted median over time windows to resist manipulation.

**Robust features** (from free 1m klines plus mark, index and premium klines):
- `close_dev = (close − bar VWAP)/ATR` and `(close − bar POC)/ATR`, where POC is the price level with the most traded volume.
- `tail_vol_share`: the volume in the last 1–5 minutes of the bar as a share of the bar's volume. A big close move on thin tail volume = a **painted close**.
- `mark_gap`: last close vs mark close, as a z-score. A spike means a print the index didn't confirm.
- `acceptance_time(L)` / `acceptance_vol(L)`: the share of 1m bars and of volume spent above level L.
- A per-symbol `close_engineering_score`: the correlation between the last-5m return and the next-5m return. Strongly negative means that coin's closes aren't to be trusted.

**Redefinitions:**
- **Sweep:** keep "the high pierces L on last AND mark". Replace "closes below L" with **rejection**: acceptance_time < 20%, acceptance_vol < 25%, and bar VWAP < L. A painted close below the level while volume says otherwise is a veto.
- **RSI and VWAP z:** compute them on mark closes or bar VWAP/HLC3, not the last close.
- **Lower highs:** use the mark high or the 95th percentile of 1m closes, so single-print wicks don't create or break the pattern.
- **Already robust:** stops trigger on MARK and outcome labels use 1m mark highs and lows.

## 2. Signal age decay
Weight each signal by age: `w = exp(−Δt·ln2/HL)`. Model "hotness" as a Hawkes-style intensity `λ ← μ + (λ−μ)e^{−Δt/τ} + α` per event, z-scored against a 30-day baseline. A signal counts as **hot** when z > 2, acceleration > 0 (fast EWMA − slow EWMA rising) and its age is under one half-life.

| Family | Prior half-life |
|---|---|
| Taker delta / order-flow imbalance | 5–15 min |
| Sweep / liquidation cascade | 30–60 min |
| News / social spike | 2–6 h |
| Funding extreme / OI build | 8–24 h |
| Basis / premium dislocation | 12–48 h |
| Pump-fade froth | 24–48 h |
| Time-series momentum | 1–3 weeks |
| Listing fade | 7–14 days |
| Unlock / delisting | event clock (−30d to +7d), not decayed |

**Calibrate from our own labels:**
1. Bucket each family's signals by age.
2. Compute the Spearman IC vs the forward short return in each bucket.
3. Fit `IC(Δt) = IC₀·e^{−Δt/τ}`.
4. Shrink toward the prior: `HL = (n·HL_fit + 30·HL_prior)/(n+30)`.
5. Refit monthly. Drop a family whose IC₀ CI includes 0.

## 3. Social and news
**Access at $0:**
- **CoinGecko** `/search/trending` (free, 10-minute cache).
- **Binance Square** `GET https://www.binance.com/bapi/composite/v4/friendly/pgc/content/queryByHashtag?hashtag=%23SOL&pageIndex=1&pageSize=20&orderBy=LATEST`. It returns contentCount, viewCount and contentCount7days. It's undocumented, so it can break and the ToS is unclear; poll politely.
- **Binance's official skills hub `crypto-market-rank`:** social-hype (hype index and sentiment) and token-rank (trending, Alpha).
- Later, read-only: public Telegram channels via Telethon (its ToS forbids AI training) and Reddit via PRAW (100 QPM, non-commercial).

**Not viable:**
- X/Twitter: pay per use at $0.005 per post read, and Nitter was archived in Sep 2026 after a cease-and-desist.
- LunarCrush: $90+ a month.
- Santiment: 30-day lag on the free tier.
- Discord self-bots: banned.

**Evidence:**
- **Attention mostly means CONTINUATION at 1–7 days:** Liu & Tsyvinski RFS 2021 (Google and Twitter attention); Da-Engelberg-Gao JF 2011 (search spikes lead to higher prices for about 2 weeks, then reversal).
- **Pump groups reliably reverse, fast:** Dhawan & Putniņš RoF 2023 (355 pumps, +65% distortion); Xu & Livshits 2019; La Morgia (detectable within 25 s); about −30% a year later.
- **Twitter sentiment is fragile:** no Granger causality in several studies, and bot-heavy posting goes with lower returns.
- **LLM sentiment:** 24h signals Granger-cause only the top-3 coins, at a lag of 6h or more.
- **Implication for shorts:** never short a still-rising attention spike. Short the **fade after attention peaks** (fast measure below slow while price is still extended).

**MVP features per symbol per hour:**
- mention count and views
- velocity z (1h vs 7-day same-hour median/MAD) and acceleration
- fast (τ ≈ 6h) / slow (τ ≈ 48h) decay ratio
- hours on CoinGecko trending
- Binance hype sentiment.

**Flag:** `hype_fade` = z ≥ 3 within 48h AND the fast/slow ratio is falling AND price is ≥ 20% above its 3-day low AND funding is positive. It's a confirmation or veto for `pump_fade`, never a standalone entry. Validate over 50+ events against matched non-hype gainers.

### 3b. On-chain shadow features (Binance Skills Hub; owner decision 2026-10-06)
**Shadow only:** logged as `oc_*` features on every scan candidate; no points, no vetoes, never a `data_missing`
input. A research review (`research_report.by_feature_tertile`) decides whether any of them earns a place in a score.

**Source:** the hub's public, keyless Binance Wallet ("web3") endpoints, called as its skills call them
(github.com/binance/binance-skills-hub @ 9960c67; a read-only copy lives outside the repo in `~/refs/`):
- `crypto-market-rank`: smart-money net inflow (`.../tracker/wallet/token/inflow/rank/query/ai`, 24h, chains BSC,
  Solana, Base) and token-rank Alpha and Trending (`.../pulse/unified/rank/list/ai`).
- `trading-signal`: smart-money buy/sell signals (`.../web/signal/smart-money/ai`, BSC and Solana).
- `query-token-info`: `search` (find the token) and `dynamic` (holders, top-10 share, KYC holders, smart-money / dev /
  insider / sniper / bundler shares, buy and sell volume by window, the Binance-routed net).

They are undocumented web endpoints: no SLA, no published rate limit (business code `100004` = rate-limited), fields
can change, and the unit of most holder shares is undocumented (logged as reported). Third-party text (names, tags)
is not kept.

**Which token is the perp's:** `search` by the base symbol, exact symbol matches on Ethereum, BSC, Base and Solana,
the deepest pool wins (never the largest market cap: clones report fake caps, e.g. a Solana "MOVR" at $3.2B cap on a
$17k pool) and only at ≥ $100k liquidity; otherwise `oc_status = no_match` and no features. Holder and supply fields
are the matched chain's, so a bridged token's top-10 share includes bridges and exchanges.

**Cadence:** the guardian's market-wide job `onchain` (every 10 min) snapshots the same coins as the social snapshot
(top 30 movers plus every coin held or resting) into the kv table; a scan only reads it (`oc_age_min` = snapshot age).
Per run: each list once per chain a matched coin lives on (at most 13 calls: 3 inflow, 2 signal, 8 rank) and at most 40
per-coin calls (a search for a new coin, then `dynamic`), within a 120 s deadline (the lane's timeout is 300 s); a match
is kept 24h. A rate limit or the deadline stops the run, which still writes what it has; a coin not reached, or whose
`dynamic` failed, keeps its earlier values and their time.

**Expect little overlap:** the smart-money lists are mostly young BSC and Solana DEX tokens; most perps will show
`oc_inflow_listed = false` and no signal. The holder structure and flows of new listings and Alpha coins are the
likeliest to carry information (the listing-fade edge).

**Legal red lines:**
- Trading on material non-public information is illegal (SEC v. Wahi; MiCA Arts 89–91).
- **Never:**
  - join or pay for "insider" groups selling leaked listings or unlocks
  - participate in or front-run pumps
  - post or amplify content to move a price we hold (the Square posting API makes this easy — don't)
  - get around paywalls or logins, or use self-bots.
- Public channels and posts only.

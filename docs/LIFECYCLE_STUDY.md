# Listing lifecycle study: how long new Binance perps take to fall, how far, and what predicts it

**Thesis tested:** "Binance lists 1-2 coins a week and they gradually turn to trash; every coin falls." Run on 2026-09-29, with data through 2026-09-28.

**Verdict:** the drift is real and robust. By day 90, 76% of new perps are below their listing price (median −44%), and half have fallen 50% from their peak by day 32. The trade is much weaker than the drift, though. A fat squeeze tail (about 1 in 4 shorts first runs **+100%** against you) turns a +30% median into a roughly +5% mean per trade, even with a hard 30% stop. The edge sits in **which** listings you short (HODLer / spot-listed at launch, and regimes where BTC is falling), not in **when** you short them.

## Reproduce
```
PYTHONPATH=src .venv/bin/python -m shortbot.lifecycle_data     # collect / top up → data/lifecycle.db (~20 min)
PYTHONPATH=src .venv/bin/python -m shortbot.lifecycle_report   # all tables below (markdown on stdout)
.venv/bin/pytest -q tests/test_lifecycle_study.py
```
Code: `lifecycle_data.py` (collection), `lifecycle_study.py` (pure per-coin metrics, tested), `lifecycle_report.py` (features, splits, tables).

## Method
- **Universe:** every USDT-margined COIN perpetual with `onboardDate` ≥ 2025-03-01 in fapi `exchangeInfo`. That list includes delisted contracts (status `SETTLING`). Contracts already purged from exchangeInfo are recovered from the data.binance.vision S3 listing. 268 symbols; 267 have ≥ 8 daily bars. **31 are delisted.** TradFi/equity/pre-IPO perps are excluded (`underlyingType != COIN`).
- **Prices:** daily klines from fapi `/klines`, one sequential call per symbol with the guardian sharing the IP. Day 0 is the listing day, and index k means k days after listing. Every coin's first bar matches its onboardDate. BTCUSDT perp is the benchmark: "minus BTC" is the coin's return minus BTC's return over the same dates.
- **Spot status:** first 1d candle of `BASEUSDT` on data-api.binance.vision:
  - `perp_only`: no spot pair.
  - `spot_old`: spot existed more than 30 days before the perp, i.e. an established coin getting a perp.
  - `spot_same`: spot opened within a day of the perp.
  - `perp_first`: spot came more than a day after the perp.
- **Listing type:** Binance CMS announcements (catalog 48 new listings, 161 delistings) matched on `(BASE)` / `BASEUSDT`, plus the exchangeInfo `underlyingSubType` tags (Alpha/Meme/AI/Layer-1…). Each coin gets one primary type, in this priority: HODLer > Launchpool > Megadrop > PreMarket > Alpha > SpotNew / PerpOnly / SpotOld.
- **Float and FDV/MC:** the guardian's CoinGecko cache (`kv.coin_supply`, read-only), which covers 231 of 267 coins. These are **current** values, not values at listing. Float and FDV/MC are near-mirror images (FDV/MC ≈ 1/float), so the two splits are redundant.
- **Metrics:** returns vs three anchors (first open, week-1 high, ATH) at 7–180 days. Days to −30/−50/−70/−90% from the running peak close and from the listing open use **Kaplan-Meier** (young coins are censored, not dropped). Also measured: pump window (day of the ATH wick, and ATH vs open), +50% dead-cat bounces after the ATH, and quote volume at day 30/90 vs week 1.
- **Short trades:** short at a day's close, exit at the close N days later. Return = 1 − exit/entry, price only: **no fees, no funding** (new listings often have negative funding, which shorts pay). MAE = worst subsequent high vs entry. "30% stop" means any trade whose MAE reached +30% is booked at −30%, with no gap slippage.
- **Delisted coins** carry their last close forward (they settled there). Dead coins stay in the sample, so there is no survivorship bias on the price side.
- **Evidence labels:** a finding is **well-supported** if n ≥ 30 distinct coins and the sign holds in ≥ 75% of listing quarters that have n ≥ 5. Anything else is an **anecdote**. Quarters are used instead of months because monthly n is 3–30.

Listings per quarter: 2025Q1 23 (March only) · Q2 64 · Q3 72 · Q4 56 · 2026Q1 28 · Q2 16 · Q3 8.

## 1. How far they fall

| horizon | n | median vs open | p25 | p75 | % below listing | median minus BTC | % below (BTC-adj) | median vs week-1 high | median N days after ATH |
|---|---|---|---|---|---|---|---|---|---|
| 7d | 267 | −12% | −38% | +20% | 61% | −10% | 61% | −39% | −49% |
| 14d | 267 | −16% | −47% | +15% | 66% | −16% | 67% | −45% | −58% |
| 30d | 263 | −24% | −53% | +9% | 70% | −24% | 69% | −50% | −66% |
| 60d | 261 | −31% | −62% | +4% | 74% | −32% | 76% | −55% | −75% |
| 90d | 259 | −44% | −68% | −3% | 76% | −40% | 75% | −61% | −79% |
| 180d | 244 | −57% | −78% | −22% | 82% | −44% | 77% | −72% | −86% |

- **Well-supported (n = 263 / 259 / 244; same sign in 6/6, 6/6 and 5/5 quarters):** new perps are below their listing open at 30d, 90d and 180d. The 90d result also holds **after subtracting BTC** (median −40%, 6/6 quarters), so this is not just beta.
- Excluding the 16 old coins changes almost nothing: the new-token subset is 90d −46% and 180d −61%.
- The p75 column shows about a quarter of coins are **still above listing at 60d**. Not every coin falls, at least not on a short's timetable.

## 2. How long it takes (Kaplan-Meier)

| drawdown | KM median days | by day 30 | by day 90 | by day 180 |
|---|---|---|---|---|
| −30% from running peak | 9 | 84% | 96% | 99% |
| **−50% from running peak** | **32** | 48% | 80% | 94% |
| −70% from running peak | 97 | 12% | 47% | 73% |
| −90% from running peak | 357 | 0% | 6% | 18% |
| −30% from listing open | 16 | 58% | 79% | 89% |
| **−50% from listing open** | **66** | 39% | 57% | 73% |
| −70% from listing open | 173 | 14% | 30% | 51% |

- **Well-supported:** −50% from the peak is a one-month process (median 32 days), and −70% takes about three months.
- −90% is rare inside six months (18%). "Trash" arrives slowly.

## 3. Pump window, bounces, volume, delisting

| | value |
|---|---|
| ATH on day 0 | 31% |
| ATH day 1–3 / 4–7 / 8–30 | 9% / 7% / 12% |
| **ATH after day 30** | **42%** |
| median ATH vs open | +101% |
| median week-1 high vs open | +30% |
| median +50% bounces after the ATH | 2 (81% have ≥ 1) |
| volume at day 30 / 90 vs week 1 | 14% / 6% |
| delisted: n, median days listed (min–max), return open→last | 31, **212 d** (95–513), −77% |

- **Well-supported:** the "controlled movement" part of the thesis is visible. **42% of coins make their all-time high more than a month after listing**, a median of +101% above the open.
  - Among Alpha-type listings it is 50%, versus 25% for HODLer.
  - This is the squeeze tail behind every short statistic below.
- **Well-supported:** volume dies fast. By day 30 a coin trades 14% of its week-1 volume, and 6% by day 90.
- **Anecdote (n = 31):** delistings come about 7 months after listing. 25 of the 31 were perp-only coins, and 15 were Alpha-type.

## 4. Splits: what predicts faster decay

| group | n | med 30d | med 90d | 90d minus BTC | % below @90d | KM days to −50% (peak) | med ATH vs open |
|---|---|---|---|---|---|---|---|
| Alpha | 90 | −25% | −58% | −40% | 80% | 23 | +142% |
| PerpOnly (non-Alpha) | 58 | −21% | −32% | −40% | 66% | 40 | +115% |
| HODLer | 53 | −31% | −51% | −43% | 83% | 26 | +74% |
| SpotNew | 26 | −10% | −26% | −24% | 70% | 72 | +100% |
| SpotOld | 15 | −20% | −27% | −44% | 67% | 104 | +58% |
| PreMarket | 15 | −47% | −56% | −29% | 73% | 21 | +45% |
| Launchpool | 9 | −44% | −61% | −74% | 89% | 30 | +44% |
| spot_same (spot at launch) | 47 | −43% | −53% | −51% | 85% | 27 | +36% |
| perp_first | 53 | −11% | −32% | −26% | 73% | 35 | +115% |
| perp_only | 151 | −23% | −44% | −40% | 75% | 31 | +123% |
| week-1 vol high (> $1.4B) | 89 | −8% | −33% | −25% | 74% | **21** | +174% |
| week-1 vol low (≤ $0.47B) | 89 | −24% | −41% | −41% | 79% | 54 | +49% |
| BTC down over first 30d | 136 | −36% | −58% | −44% | 87% | **21** | +82% |
| BTC up over first 30d | 131 | −15% | −25% | −35% | 65% | 46 | +112% |

Spearman ρ, using only features known by day 7, against "−50% from peak within 60 days" (n ≈ 263):

| feature | ρ |
|---|---|
| log first-week volume | +0.37 |
| BTC 30d | −0.24 |
| old coin | −0.20 |
| Alpha | +0.18 |
| float | −0.18 |
| FDV/MC | +0.17 |

Meme and AI tags are near 0.

What the splits show:
- **Well-supported:** **big first-week volume means a faster collapse from the peak** (ρ +0.37; KM 21 vs 54 days). Big listings pump harder (+174%) and then halve faster.
- **Well-supported:** **market regime dominates raw returns.** When BTC fell over the coin's first 30 days, the median 90d return was −58%, versus −25% when BTC rose. The BTC-adjusted gap is much smaller (−44 vs −35), so much of "every coin falls" is beta in a falling tape.
- **Well-supported (weak effect):** low float / high FDV/MC coins reach −50% slightly faster (ρ ≈ 0.17). Float does **not** predict the 90d return (ρ ≈ 0), and it is measured today, not at listing.
- **Anecdote (small n):**
  - Launchpool (n = 9) and PreMarket (n = 15) fall fastest.
  - Old coins given a perp decay slowest (KM 104 days).
  - Meme (n = 14) pumps most (+198% ATH) and is the one tag where a d7 short lost money at the median.
- **Hindsight, not predictors:** the ATH size and ATH day correlate ρ ≈ 0.6 with the 90d return, but only because the ATH can come after day 90. Don't use them ex ante.

## 5. When to short: entry anchor × holding period (all 267 coins)

| entry (close of) | hold | n | median short ret | win % | median MAE | p90 MAE | MAE ≥ 100% | mean, no stop | **mean, 30% stop** |
|---|---|---|---|---|---|---|---|---|---|
| day 0 | 30 | 263 | +29% | 73% | +29% | +179% | 21% | −0.1% | +5.0% |
| day 1 | 30 | 263 | +25% | 72% | +30% | +171% | 20% | −1.5% | +4.2% |
| day 3 | 30 | 263 | +24% | 70% | +27% | +153% | 18% | +2.9% | +5.0% |
| **day 7** | **30** | 263 | +21% | 72% | +23% | +145% | 18% | +7.8% | **+6.3%** |
| **day 7** | **60** | 261 | +32% | 75% | +33% | +235% | 25% | +12.7% | **+5.7%** |
| day 14 | 30 | 263 | +18% | 70% | +27% | +164% | 17% | +3.6% | +0.9% |
| day 30 | 30 | 261 | +14% | 67% | +22% | +137% | 14% | −3.6% | +3.7% |
| day 60 | 30 | 259 | +11% | 63% | +23% | +136% | 14% | −4.3% | +1.1% |
| first close < week-1 low | 30 | 253 | +18% | 73% | +25% | +142% | 15% | +6.8% | +2.5% |
| **first close −30% off peak** | 30 | 259 | +20% | 69% | +26% | +184% | 20% | +9.7% | **+6.1%** |
| first close −30% off peak | 60 | 256 | +33% | 74% | +33% | +270% | 26% | +10.3% | +6.2% |
| *week-1 high (hindsight)* | 30 | 263 | +51% | 89% | 0% | +54% | 5% | +37% | +39% |
| *ATH (hindsight)* | 30 | 251 | +66% | 100% | 0% | 0% | 0% | +64% | +64% |

The mean with a 30% stop, by listing quarter:

| trade | Q1 '25 | Q2 | Q3 | Q4 | Q1 '26 | Q2 |
|---|---|---|---|---|---|---|
| day 7 → 60d | +3% | +6% | +1% | +12% | +5% | +7% |
| day 7 → 30d | +11% | +8% | +3% | +12% | +1% | −1% |
| −30% off peak → 30d | +11% | +5% | +6% | +8% | +11% | −8% |
| day 0 → 30d | +9% | +6% | −1% | +13% | +3% | −5% |

- **Well-supported (n = 261, positive in 6/6 quarters):** the best simple anchor is **short the day-7 close and hold 60 days, with a hard stop.**
  - Median +32%, win 75%, mean +5.7% with a 30% stop.
  - Squeeze risk: median MAE +33%, p90 MAE +235%, and **25% of trades go +100% against you first.**
  - Close runner-up: the "first close 30% below the running peak" trigger (+6.1–6.2%, 5/6 quarters).
- **Early is not better.** Shorting day 0–3 has similar medians but worse squeeze tails (MAE ≥ 100% in 18–21% of trades at 30d) and a lower stopped mean. After day 14 the edge fades: the move has partly happened and the late ATHs still arrive.
- **Well-supported:** **unstopped shorts are a coin flip on the mean** even though the median is strongly positive. A few coins go +500–1000% (d1→90d mean −117%). A position that can't survive +100% needs either a hard stop or small size.

### The squeeze tail depends on the listing type (short day 7 → 60d)

| group | n | median | win % | MAE ≥ 100% | mean, 30% stop |
|---|---|---|---|---|---|
| **HODLer** | 53 | +47% | **91%** | 17% | **+20%** |
| spot_same (spot at launch) | 47 | +39% | 83% | 17% | **+19%** |
| SpotOld | 15 | +18% | 87% | 0% | +18% |
| week-1 vol high | 88 | +43% | 85% | 24% | +12% |
| BTC down over first 30d | 132 | +42% | 82% | 23% | +10% |
| perp_only | 147 | +30% | 68% | **32%** | +0.2% |
| **Alpha** | 90 | +31% | 70% | **40%** | **−2.9%** |

- **Well-supported (n = 53, stopped mean positive in 4/5 quarters that have n ≥ 3):** HODLer-airdrop listings and coins that open spot at the same time as the perp are the clean shorts.
  - They are the most likely to have their ATH on day 0 (HODLer: 42%).
  - Only about 1 in 6 squeeze +100%.
- **Well-supported (n = 90):** Alpha / perp-only listings fall just as far eventually (90d −58%), but **half make their ATH after day 30.** A d7 short is squeezed +100% 40% of the time, and **the 30% stop turns it into a loser.** Reading: these are the "controlled" coins. Short them only after the late pump has visibly broken, or not at all.
- **Anecdote:** the mid-volume tertile and mid-float tertile have the worst tails. There is no clear mechanism, so treat it as noise until confirmed.

## Implications for the desk (hypotheses to test, not rules yet)
1. `listing_fade` has a real edge when it is **selective**: prefer HODLer / spot-at-launch / high week-1 volume, enter around day 7 (or on the first close 30% off the peak), hold 30–60 days, use a hard stop, and size for a +30% adverse move.
2. Treat **Alpha / perp-only** new listings as a *late-pump risk*, not a fade: `pump_fade` after a confirmed turn, never a day-7 blind short.
3. Regime matters: the same short earned +10% (stopped mean) when BTC was falling, versus +1.5% when BTC was rising.
4. The first month is where the volume is (day-30 volume is 14% of week 1). Liquidity for exits disappears fast, and so does the squeeze fuel, except on Alpha coins, where the thin book *is* the squeeze.

## Caveats
- **One regime:** 18 months, mostly a weak altcoin market. BTC-adjusted numbers help, but "everything falls" is partly the tape. n by quarter is small in 2026 (28, 16, 8).
- **Survivorship:** prices include delisted perps (carried forward). Float/FDV come from today's CoinGecko top-2000 and miss 36 mostly-dead coins. Tags come from today's exchangeInfo and announcements, and title matching can miss or mis-tag a coin.
- **Costs ignored:** fees, **funding** (often strongly negative on new listings, which shorts pay daily), slippage, and gaps through stops. A 30% stop on a +100% daily candle fills far worse than −30%. The stopped means (+5–20%) are upper bounds.
- **Daily bars:** the day-0 open can be a pre-market or auction print, and intraday entries and squeezes are invisible. Historical market cap (CoinGecko `market_chart`) was not sampled, so float is not float-at-listing.
- **Small n per tag:** Launchpool (9), Megadrop (1), Meme (14), PreMarket (15) and SpotOld (15) are anecdotes.
- Not investment advice. This measures the past, and the desk's `research_report` / trade stats remain the source of truth for live trades.

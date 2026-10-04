# How professional quants make money, and what we copy (research 2026-10-01)

These are benchmarks and structural lessons from pro crypto quant teams, crypto funds, market makers and TradFi quant firms. The strategies themselves are in [strategies/README.md](strategies/README.md). This file updates the 2026-09-28 conclusions in [STRATEGY.md](STRATEGY.md).

No firm publishes its signals. The numbers below describe categories, and every source has a bias:
- opt-in team samples;
- venue-sponsored reports;
- self-reported track records;
- survivorship.

Treat them as upper bounds.

## 1. Realistic benchmarks

### 1Token crypto quant strategy index
Source: https://blog.1token.tech/, 6-13 volunteer teams a month. Values are annualised monthly means across teams, net of trading costs and funding, probably gross of management fees.

| Month | Funding arb | Long-short (dollar-neutral) | Directional (4 teams) |
|---|---|---|---|
| Jul-2025 | 16.3% | +28.9% | – |
| Aug-2025 | 8.0% (worst team -11.9%) | +24.0% | – |
| Sep-2025 | 10.0% | -58.8% | – |
| Oct-2025 (10-Oct crash) | 7.2% (worst -10.4%) | **+49.8%, every team positive** | – |
| Nov-2025 | 6.2% | -4.1% | – |
| Jan-2026 | 7.5% | +40.6% | **+111.9%** |
| Feb-2026 | **2.8% (< T-bill)** | **-118.8%, every team negative** | -35.8% |
| Mar-2026 | 4.8% | -21.4% | -59.5% (every team negative) |
| Apr-2026 | 7.7% | +52.3% | -119.3% (every team negative) |
| May-2026 | 6.4% | +41.9% | -75.9% |

Risk profile by strategy type:

| Type | Monthly max DD | Vol (ann.) | Other |
|---|---|---|---|
| Funding arb | 0.2-0.6% (worst team 1.75%) | – | implied excess Sharpe fell from ~4.4 (Jul-2025) to ~1.5 (May-2026) |
| Long-short | 4-12% (worst 18%) | 12-38% | holds 1-2.5 hours to ~4 days |
| Directional | 8-12% (worst 18%) | 19-43% | win rate ~40%, payoff 0.77-0.98 in a losing month; roughly -10% to -15% YTD by May-2026 |

The full 2025 annual report (25 teams, >$10B AUM, >50 strategies) is gated on DocSend.

### Strategy-type returns by year
Source: Galaxy VisionTrack, fund-reported (https://visiontrack.galaxy.com/indices/).

| Index | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 YTD (Aug) |
|---|---|---|---|---|---|---|
| Market neutral | +46.6% | -2.6% | +15.5% | +22.3% | +13.1% | +6.2% |
| Quantitative | +105.8% | -26.6% | +55.9% | +60.8% | -7.5% | -4.2% |
| Fundamental (discretionary) | +254.7% | -57.3% | +98.6% | +51.9% | -24.9% | -0.9% |
| Bitcoin | +59.6% | -64.2% | +155.9% | +121.1% | -6.4% | -10.2% |

Crypto Fund Research, 2025 (https://cryptofundresearch.com/best-performing-crypto-funds/):
- The average crypto hedge fund returned -7.2%.
- Quant was the only positive category, at +0.4% (median +3.2%; 58% of funds positive).
- 117 quant funds report a since-inception median Sharpe of 1.53, median max DD -20.7% and median BTC beta 0.10. These figures are self-reported and survivor-biased.

**Ignore** the content-farm "2025 quant +48%" figure: it contradicts both sources.

### What this means for our targets
- A directional desk with Sharpe ~1 over a year and a 10-20% peak drawdown would be in line with professionals.
- Losing months in a row are normal, even for pros.
- Any directional Sharpe above 3 is not credible on this evidence.
- The 2026-09-28 plan of **net Sharpe 0.5-1.0** stands. It is if anything optimistic for a directional-only book.
- For sizing, plan around a ~40% win rate.

## 2. Where the money comes from
Ranked by how reliably each source pays:

1. **Being the liquidity: market making, rebates and OTC flow.**
   - Jane Street made $39.6bn revenue in 2025 with ~3,500 staff (~$11.3m per head), and $10.3bn profit in Q1-2026 alone.
   - Citadel Securities made $12.2bn (~$6.8m per head); HRT made $12.3bn.
   - Wintermute made $1bn revenue and $582m profit in 2021, then only $225m revenue in the first 9 months of 2022. Revenue swings ~-75% from peak to trough with volume. Wintermute now targets more than 50% of revenue from outside crypto.
   - **Closed to us.**
2. **Neutral funding and basis.** BTC/ETH spot-perp carry is now about the T-bill rate:
   - Ethena funding fell from ~11% (2024) to ~5% (2025), with sUSDe at ~3.7% in early 2026.
   - Liquibit runs less than 20% of its book in spot-perp "due to retail competition".
   - The edge moved to perp-vs-perp **dispersion** across venues and coins (Liquibit ~10% gross at 3-6x leverage) and to TradFi-linked perps (funding ~12.6% vs ~2.8%).
   - Sygnum's institutional BTC carry fund targets 8-10%.
3. **Balance-sheet and special-situation deals.** Jump made ~$1.28B from Terra and later paid a $123M SEC settlement. Token-project deals:
   - **Loan + call:** the project lends ~1% of its supply to the market maker for 12 months, along with call options.
   - **Discount OTC buys:** DWF buys at 5-15% below market.

   These deals create the **structural sell flow** in new listings, which is what our listing_fade and unlock setups trade. **Closed to us** as a business, but open as a short.
4. **Systematic directional** (trend, cross-sectional, carry tilts) is the minority sleeve.
   - Amphibian (a crypto quant fund of funds) allocates 60-65% market-neutral, 20-25% market making and arbitrage, and 20-25% L/S + trend + mean reversion.
   - Net long never exceeds 10-20%.
   - Directional risk is held "for a limited period".
   - **Our whole desk sits in this sleeve**, so expect pro-directional volatility, not neutral-book smoothness.
5. **Discretionary calls by an LLM: no demonstrated edge.** In Nof1 Alpha Arena, 4 of 6 models lost 31-63% in Season 1, and 26 of 32 accounts lost in Season 1.5 (-35% overall). The Season 1 winner lost in all 4 of its later runs.

## 3. Why the winners are consistent (structure)
1. **Many small, weakly correlated edges combined into one model.**
   - Medallion is right 50.75% of the time over 150-300k trades a day, holding 1.5 days to 2.5 weeks, with one combined model.
   - A signal is admitted only if it is significant, stable across sub-periods and non-random.
   - Pod shops run 300+ independent teams.
2. **Capacity discipline.** Medallion is closed and capped. In 2020 it made +76% while RenTech's high-capacity fund RIEF lost ~-20%. Edge shrinks with size.
3. **Hard, mechanical kill rules.**
   - Millennium cuts a team's capital in half at -5% and closes the pod at -7.5%. PM turnover runs 15-20% a year.
   - Medallion de-risks automatically when volatility spikes.
4. **Capital follows realised, risk-adjusted P&L.** Allocation grows or shrinks monthly by results.
5. **Position where others are forced to trade:** ETF create/redeem, retail flow, index rebalances, liquidations, listings. This same edge fails when it gets crowded:
   - Millennium lost ~$900m on index add/delete bets (Feb-2025).
   - In the Aug-2007 quant quake, similar books deleveraged together.
   - In summer 2025 RIEF lost -6% in June and -5.1% in July.
6. **Venue risk is a first-class risk.**
   - Monthly exchange-exposure questionnaires (Amphibian).
   - 2x minimum margin per exchange, because "exchanges don't make margin calls, they just liquidate you" (Liquibit).
   - Single opportunistic trades capped at 20% of NAV.
   - None of the 2022-26 fund deaths read here came from alpha decay. The causes were leverage plus correlated directional bets dressed as arbitrage (3AC), venue failure (FTX) and balance-sheet rescues (Jump/Wormhole).
7. **Costs decide.**
   - Industry fees took ~50% of gross gains over the last two decades.
   - Balyasny went from 15.2% gross to 2.8% net in 2023.
   - Multi-strat investors kept 41c of each gross dollar.
   - Our equivalent drag is exchange fees, slippage, funding and LLM/infra costs. Robot Wealth assumes 15 bp per trade and rebalances only past a no-trade buffer.
8. **Research process.**
   - Start from "who is price-insensitive and why can I exploit it" (Robot Wealth).
   - Universe and selection filters carry most of the result: tier-1 pairs reach Sharpe 1.6 vs 0.42 for tier-5.
   - Retire a strategy when its venue or mechanism dies (Robot Wealth retired its FTX strategies), not only on drawdown.

## 4. How much to learn from each
| Model | Copy? | Practices to copy | Don't copy |
|---|---|---|---|
| Pod shops (Millennium, Citadel, Balyasny) | **High** | drawdown ladder (-5% halve, -7.5% stop new entries); each setup as its own "pod" with capital, P&L and kill rule; monthly reallocation by risk-adjusted P&L; gross vs net per setup | 6-7x leverage; crowded calendar bets; headcount cost |
| Crypto allocators / neutral funds (Amphibian, Liquibit, Sygnum) | **High** | venue-risk checklist; USDT/USDC-only margin; ≥ 2x margin buffer; cap any single trade; time-box directional risk; carry as a regime signal | multi-venue funding arb (needs venues and capital) |
| Trend CTAs (Man AHL, Winton, AQR managed futures) | **High (signal)** | time-series trend as a regime veto ("no shorts in up-trends"); vol-scaled sizing; multi-lookback signals | 100-market diversification (all perps are ~one factor); monthly cadence |
| Small-team quants (Robot Wealth) | **High** | a few fixed-weight signals (0.5 carry / 0.2 momentum / 0.3 breakout); daily cadence; no-trade buffer; 15 bp cost model; mechanism-based retirement | members-only lookbacks (rebuild ourselves) |
| RenTech / Medallion | Med | one combined score; signal admission tests; auto de-risk on vol spikes; stay small where the edge lives | 150k trades/day; unexplainable signals without a huge sample |
| AQR factor investing | Med | cross-sectional ranking; factor-crowding awareness | long-only beta tilts |
| Jane Street | Med (culture) / Low (business) | score the system on desk-wide P&L; medium frequency (minutes to days) is where their 2026 growth came from, which matches our horizon | ETF/MM franchise; anything like the SEBI expiry pattern (moving a thin index to benefit a derivative) |
| Crypto MMs (Wintermute, GSR, HLP) | Low (business) / High (lessons) | rest deep reduce-only TPs to absorb cascade wicks; maker entries; expect -75% volume cycles | quoting, rebates, project deals |
| Citadel Securities, HRT | Low | be where flow is forced; measure execution cost per fill; days-weeks horizons beat a latency race | PFOF, DMM, sub-second MM |
| LLM discretionary traders (Alpha Arena) | **Anti-model** | low turnover and sizing beat direction calls; the LLM runs research and ops | LLM judgement as the alpha |

## 5. The 10 October 2025 crash: what to engineer for
- **Scale:**
  - $19.3bn was liquidated in 24h, 87% of it longs.
  - ~$7bn went in under 1 hour.
  - Open interest fell from $175B to $125B.
- **ADL closed profitable shorts.** Binance ranks positions for ADL by PnL% × effective leverage.
  - A big winning short at high effective leverage is first in line.
  - Bank partials and keep effective leverage low as unrealised profit grows.
  - `GET /fapi/v1/adlQuantile` shows the position's rank.
- **Collateral:** Binance marked USDe, BNSOL and WBETH from its own order book, so USDe printed $0.62-0.65 and hedged accounts were liquidated. Binance paid ~$283m in compensation plus a $400m programme.
- **Liquidity:** market makers were locked out for ~2h and pulled liquidity for the quarter (the thinnest books since 2022). BTC traded 5-10% apart across venues.
- **Wicks:** ATOM printed $0.001, TON -80%, WLD -70%. Resting deep bids filled; stops filled at absurd prices.
- **Who profited:** relative-value books (every 1Token long-short team was positive in October) and liquidation backstops (HLP +$40m). Funding-arb books lost.
- **Rules for us:**
  - margin in USDT/USDC only;
  - ≥ 2x margin buffer;
  - per-coin size capped against OI and depth;
  - exchange-side stops;
  - deep reduce-only TP ladders on shorts;
  - partial profits into cascades;
  - no new shorts right after a long-liquidation cascade.

## 6. Corrections to our 2026-09-28 conclusions (STRATEGY.md)
1. **"Cash-and-carry is closed"** holds for BTC/ETH spot-perp. Carry itself moved: to dispersion and TradFi perps for the yield, and into our toolbox as a crash gate and a ranking signal.
2. **"Specialise"** should mean specialising in a mechanism (supply, listings, funding dispersion), not in a direction. The cleanest forced flow, forced selling, is a buy. A short-only book is the sleeve pros size smallest.
3. **"Small fixed size protects us"** is necessary but not sufficient. Tail losses came from venue mechanics (ADL, collateral marking, lockouts), which scale with leverage and alt thinness.
4. **AI judgement is not the edge.** Move judgement into explicit, backtestable rules and keep the agent on research and execution. This does not argue for approval gates.
   - **Superseded on our own evidence (2026-10-04):** the demo journal measured the AI's discretionary exits and declines as the desk's edge, and rule-based replays of the same pipeline lost. The owner kept the AI's full authority. Research informs the AI and never replaces it (QUANT_METHODS.md "Design principle").

## Sources
- **1Token monthly indices** (IV Jul-2025 to XIII May-2026), the 2025 annual report, TradFi perpetual arbitrage, Crypto Fund 101 funding-fee arbitrage: https://blog.1token.tech/
- **Index and fund data:**
  - Galaxy VisionTrack: https://visiontrack.galaxy.com/indices/
  - Crypto Fund Research: https://cryptofundresearch.com/
  - PwC/AIMA Global Crypto Hedge Fund Report 2025
- **Crypto funds and allocators:**
  - Sygnum (Cointelegraph);
  - Moon Pursuit (CoinDesk 2025-12-17);
  - Amphibian, Liquibit (The Hedge Fund Journal);
  - Selini (Blockcrunch, PANews).
- **Market makers and Ethena:**
  - Wintermute (The Block, CoinMarketCap);
  - GSR (Ledger Insights);
  - DWF deals (The Block, CoinDesk);
  - Hyperliquid HLP (CoinGecko);
  - Ethena (Stablecoin Insider).
- **10 October 2025:** insights4vc, CoinShares, CoinGecko, BitMEX via CoinDesk 2026-01-08, Forbes 2025-10-21, Binance ADL FAQ.
- **TradFi firms and funds:**
  - Reuters via MarketScreener (Jane Street, Citadel Securities, HRT);
  - IFR; Wikipedia (Jane Street, RenTech, HRT, Citadel Securities, Man Group);
  - Fortune 2026-01-02;
  - Yahoo Finance;
  - Hedgeweek (RenTech, Millennium, pass-through fees, emerging managers);
  - Net Interest "Peak Pod";
  - Spear's / LCH Investments.
- **Academic and practitioner:**
  - Khandani-Lo (NBER w14465);
  - AQR "A Century of Evidence on Trend-Following";
  - BIS WP 1087;
  - Robot Wealth (index of strategies, alpha combination, turnover, carry, 2026 essays);
  - Nof1 Alpha Arena (iweaver, GitHub compilation).
- **Not obtained:**
  - the DocSend 2025 annual report (403);
  - 2025 SG Trend / Man AHL returns;
  - the HFR/Eurekahedge crypto indices;
  - internals of GSR, Amber, Kronos, Presto, Gravity and QCP.

  The session web-search budget ran out.

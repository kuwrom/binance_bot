# Carry, arbitrage and market-neutral strategies (library, research 2026-10-01)

Format and status rules: [README.md](README.md). Most of these need a spot leg, a second venue or a fee tier we don't
have; they are kept for the record and as **features** (funding regime, premium, interval switches) for the short book.

**The decay in one line:** mainstream funding carry went from ~30%/yr (2021) to 16.3% (1Token funding-arb index,
Jul-2025), 6-8% (Oct-2025 to May-2026) and 2.8% in Feb-2026, below T-bills. BTC carry Sharpe 6.45 (2020-25) fell to
4.06 (2024) and turned negative in 2025 (arXiv 2510.14435). The market price of plain carry is high single digits net:
Sygnum's institutional BTC carry fund targets 8-10%. The premium has moved to TradFi-linked perps (~12.6% ann. vs ~2.8%).

### funding_cash_carry — Spot-perp funding carry (cash-and-carry)
- direction: neutral
- family: carry
- mechanism: leveraged retail longs pay funding to whoever is short the perp; balance-sheet capacity to hedge it is limited, so the hedged short is paid for supplying it and for holding squeeze, haircut and ADL tail risk
- rules: equal-quantity long spot / short USDT-M perp in one portfolio-margin account; one leg maker, one taker; enter when 7d average funding annualises > 10-15% after fees, exit when the 3d average < ~3% or negative; margin buffer >= 3x; rebalance daily to keep delta < 2%. Active style rotates into the highest predicted funding (30-50% daily turnover); passive style forecasts days to weeks ahead (< 10%/day)
- data needed: /fapi/v1/fundingRate, /fapi/v1/premiumIndex, spot klines: Binance public; needs a SPOT account and portfolio margin
- evidence: 1Token funding-arb index (6-9 teams) mean ann. 16.26% (Jul-2025), 10.04% (Sep), 7.23% (Oct), 2.82% (Feb-2026), 6.43% (May-2026); mean monthly max DD 0.17-0.62%, worst team 1.75%; worst-team months -11.9% (Aug-2025) and -10.4% (Oct-2025) ann. (https://blog.1token.tech/crypto-quant-strategy-index-xiii-may-2026/). BTC USDT-perp carry 14.26% ann., vol 1.63%, Sharpe 8.76, 2020-08 to 2023-06 (Christin-Routledge-Soska-Zetlin-Jones, https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4268371). Galaxy VisionTrack market-neutral index +46.6% (2021), -2.6% (2022), +15.5%, +22.3%, +13.1% (2025), +6.2% (2026 YTD) (https://visiontrack.galaxy.com/indices/). Sygnum BTC Alpha 8.9% ann. in BTC terms, Q4-2025 (https://cointelegraph.com/news/swiss-bank-sygnum-raises-750-btc-market-neutral-fund). Ethena (the industrialised version): BTC/ETH funding ~11% (2024) -> ~5% (2025), sUSDe ~3.7% early-2026, USDe $14B -> $5.9B (https://stablecoininsider.org/ethena-usde-q1-2026-report/); Liquibit cut spot-perp to < 20% of its book "due to retail competition" **Repo study 2026-10-04:** Carver expresses carry in Sharpe units (annualised carry / (16 x daily vol)), smoothed over 10-125 days; for perps, carry = -annualised funding / annualised volatility (learning-repos/pysystemtrade (Carver, GPL-3: formulas reimplemented, never copied), repo study 2026-10-04).
- grade: Practitioner-data
- risks: crowding (Ethena ~$14B); perp overshoots spot in squeezes (Gate POGAI: a 15-20% gross position became 60-70% after a 10x pump); ADL closes the hedge leg (10 Oct 2025); collateral depeg (USDe $0.62-0.65 on Binance only); haircuts; venue failure (FTX)
- fit: Med — about rf + 2-4% in 2026 and needs a spot leg; useful as idle-margin parking and as a funding-regime sensor

### xs_funding_carry_basket — Cross-sectional funding carry basket
- direction: neutral
- family: carry
- mechanism: funding is sticky; high-funding perps are crowded leveraged longs paying shorts, low or negative funding marks crowded shorts
- rules: daily z-score of trailing-24h funding across the top-30 perps by 30d volume; short the highest, long the lowest (or long spot); Robot Wealth blend 0.5 carry + 0.2 10d momentum + 0.3 20d breakout, no-trade buffer, 15 bp cost model. Short-only variant: short the top-funding decile with a price trigger
- data needed: fundingRate history, klines: Binance public
- evidence: Binance perps 2019-2024: carry decile returns noisily linear, stronger in the tails; "has gotten harder as more people chase it" (https://robotwealth.com/quantifying-and-combining-crypto-alphas/). 45-symbol basket, 76 walk-forward windows: +48.6% net (~9.2%/yr, Sharpe 1.17) in development, but HOLDOUT Sep-2025 to Aug-2026 -0.5% (Sharpe -0.02); a shuffled-coin null still made +28%, so it is a market-wide premium, not coin selection (https://dev.to/pavel_kkkkazantsev/three-crypto-edges-three-different-killers-1i6o) **Repo study 2026-10-04 (negative, out of sample):** Jansen's Binance-perps case study ranked 19 perps every 8h on premium, funding and volatility. The chosen setup (1 of 2,807 candidates) had a validation Sharpe of 1.67 (PSR p 0.005), but a Deflated Sharpe below 0 under every trial count, then a 2024-25 holdout Sharpe of -0.83 (CAGR -45%, max DD -82%); execution costs ran more than 4x the funding earned (learning-repos/machine-learning-for-trading/case_studies/crypto_perps_funding (Jansen, MIT)). Carver's relcarry (smoothed carry minus the class median) is the cross-sectional version (learning-repos/pysystemtrade (Carver, GPL-3: formulas reimplemented, never copied), repo study 2026-10-04).
- grade: Practitioner-data
- risks: the short leg on top-funding alts is squeeze risk (funding is high because price is ripping); carry compression after 2024
- fit: Med — Binance-native; ranks short candidates by who pays funding, but failed out of sample as a book

### perp_spot_basis_convergence — Perp-vs-spot convergence arbitrage
- direction: neutral
- family: stat-arb
- mechanism: the perp deviates from its no-arbitrage value (spot x funding/interest wedge, Binance clamp); leveraged longs push it rich, capital-constrained arbs pull it back within hours
- rules: hourly rho = annualised deviation of the perp from clamp-adjusted fair value; if rho > the fee-tier threshold short perp / long spot (or reverse); close when rho first returns to 0; average hold 3-11h; BTC/ETH/BNB/DOGE/ADA
- data needed: premiumIndex, klines, fundingRate: Binance public
- evidence: Binance hourly 2020-01 to 2024-03: BTC Sharpe 11.65 at zero fee, 3.35 at a retail fee tier, 3.27 after effective spread (12.6% ann., max DD -2.6%); ETH 4.45 net; BTC return 49.6% (2020) -> 21.4% (2024). Sharpe is annualised on active hours only, so inflated (He-Manela-Ross-von Wachter, https://arxiv.org/abs/2212.06888)
- grade: Working-paper
- risks: needs a spot leg; fee tier dominates; thin on alts; decaying every year
- fit: Med — clean hourly public signal; usable now as a short-perp timing filter when rho is high

### quarterly_basis_calendar — Quarterly futures basis / calendar spread
- direction: neutral
- family: carry
- mechanism: dated futures trade at a premium paid by leverage demand without funding risk; the premium converges at expiry, so the yield is locked at entry
- rules: short the BTCUSDT/ETHUSDT quarterly, long perp or spot, when annualised basis minus expected funding > 8-10%; hold to expiry or close below 2-3%; reverse calendar when the quarterly trades at a discount in panics
- data needed: /futures/data/basis, delivery klines: Binance public
- evidence: crypto carry averages > 10% a year and reaches ~60%; high carry predicts crashes (Schmeling-Schrimpf-Todorov, BIS WP 1087, https://www.bis.org/publ/work1087.htm); cash-and-carry Sharpe ~0.6 pre-cost; the futures leg is liquidated in > 50% of months at 10x
- grade: Working-paper
- risks: basis widens before converging (mark-to-market margin); capital-heavy; ETF/CME basis traders compress it
- fit: Low — capital per $ of profit is high; the quarterly-vs-perp premium is a free sentiment feature

### funding_interval_switch — Funding interval switch (8h -> 1h) as squeeze-stage feature
- direction: neutral
- family: carry
- mechanism: when a symbol's funding hits the cap, Binance moves it to hourly funding within ~15 min and back only after 16 consecutive hourly prints with |rate| < 0.025%; the per-interval cap stays, so daily carry can jump 8x and trapped longs pay shorts hourly
- rules: flag symbols whose interval drops below 8h with funding at the cap; a hedged short collects up to 8x per day; unhedged, enter only with a squeeze_exhaustion trigger; exit as the 16-print revert nears or funding < 0.03%. F = [P + clamp(0.01% - P, +-0.05%)] / (8/N)
- data needed: /fapi/v1/fundingInfo (interval, caps), premiumIndex (predicted rate): Binance public; past intervals inferable from the spacing of fundingRate timestamps
- evidence: rules from the Binance funding FAQ (https://www.binance.com/en/support/faq/introduction-to-binance-futures-funding-rates-360033525031); no published backtest of the trade
- grade: Single-source
- risks: these symbols are mid-squeeze; funding can flip deeply negative after the squeeze
- fit: Med — a free feature for funding_hi / squeeze_exhaustion; our veto #3 already uses the 1h interval

### coinm_usdm_funding_spread — USDⓈ-M vs COIN-M funding spread
- direction: neutral
- family: carry
- mechanism: USDT-margined and coin-margined books have different users, so funding diverges
- rules: long the cheaper-funding contract, short the richer; rebalance the hedge as the COIN-M USD-unit ratio drifts with price
- data needed: /fapi/v1/fundingRate and /dapi/v1/fundingRate: Binance public
- evidence: mention only (https://blog.1token.tech/crypto-fund-101-funding-fee-arbitrage-strategy/)
- grade: Single-source
- risks: small spread; coin-M convexity; thin COIN-M liquidity
- fit: Low — small edge, needs coin collateral

### cross_venue_spread_arb — Cross-venue price and funding spread arbitrage
- direction: neutral
- family: stat-arb
- mechanism: the same perp trades at different prices and funding on different venues because collateral cannot move instantly (Hyperliquid retail long-skew, 1h settlement; BNB on Binance has a 0% baseline); the arb is paid for bridging fragmented capital
- rules: long the cheap venue, short the rich one when the annualised spread > ~10-15% net of four legs of fees; exit below ~3% or on a flip; equal notional; monitor both margins
- data needed: Binance public plus other venues' public APIs (Hyperliquid, Bybit, OKX); a funded account on each
- evidence: Hyperliquid-Binance BTC/ETH funding spread averaged 5.98-11.4% APR, peaks > 23%, past cases up to 48% (https://medium.com/boros-fi/cross-exchange-funding-rate-arbitrage-a-fixed-yield-strategy-through-boros-c9e828b61215, vendor); Liquibit (160 coins x up to 5 instruments, 3-6x leverage, 2x minimum margin per venue) made ~10% gross in 2025 on perp-vs-perp funding dispersion, negative in 2022 on FTX (https://thehedgefundjournal.com/liquibit-market-neutral-crypto-strategy-traditional-trading/); on 10 Oct BTC was 5-10% apart across venues "because nobody could move capital" (https://insights4vc.substack.com/p/inside-the-19b-flash-crash); Sygnum runs it as part of its 8-10% target
- grade: Single-source
- risks: transfer delays, venue failure (Bitget $351.6M wallet breach, 30 Sep 2026), one leg liquidated in a gap, latency competition
- fit: Low — shortbot is single-venue by design; HL-vs-Binance funding divergence is usable read-only as sentiment

### tradfi_perp_carry — TradFi-linked perpetual carry (stocks, ETFs, commodities)
- direction: neutral
- family: carry
- mechanism: crypto speculators pay persistent long funding on stock/ETF/commodity perps; few desks can hold the long hedge across broker, tokenised stock or chain, so the premium pays for that friction and leg-failure risk
- rules: short the TradFi perp, long equal notional underlying (IBKR, Binance b-stocks, on-chain); cross-venue leverage ~1-2x; size to stressed perp depth; cut exposure around earnings, weekends and macro; pre-position collateral where losses will land
- data needed: Binance stock-perp funding and marks: public; hedge-leg prices external
- evidence: robust mean funding ~12.6% ann. on TradFi perps vs ~2.8% on mainstream crypto perps (20 Jun-10 Jul 2026, Binance/Bitget/OKX/Hyperliquid); worked net carry ~7.5% (12% gross - 2% financing - 1% execution - 1.5% idle collateral); managers target 15-25% (ForthTech 2026 YTD 6.27%, max DD 0.66%; HashRock 7.42%, max DD 4.19%) (https://blog.1token.tech/tradfi-perpetual-arbitrage/)
- grade: Practitioner-data
- risks: legs break asymmetrically (an after-hours earnings gap liquidates the perp short while the broker gain is not usable margin); weekend gaps; oracle definitions; track records "measured in weeks"
- fit: Med — the short leg suits us; unhedged it is naked equity shorting with gap risk; at minimum a carry-tailwind filter for stock-perp shorts

### listing_basis_arb — New-listing basis arbitrage
- direction: neutral
- family: event/supply
- mechanism: at the listing of a new tokenised stock or perp, price discovery is thin and perp longs overpay; arbs buy spot, short perp and collect convergence, funding and maker rebates
- rules: long spot / short the matching perp right after listing, maker execution, exit on convergence; thresholds not published
- data needed: listing announcements, exchangeInfo onboardDate, spot/perp prices and funding: Binance public
- evidence: LuxTech strategy description, no performance (https://blog.1token.tech/tradfi-perpetual-arbitrage/)
- grade: Single-source
- risks: tiny capacity; listing-day squeezes on the short leg
- fit: Med — the directional analogue (short an overpriced new perp once funding/basis is rich) is our listing_fade

### pairs_copula_cointegration — Hourly cointegrated pairs with a copula mispricing index
- direction: neutral
- family: stat-arb
- mechanism: cointegrated coins diverge on idiosyncratic flow and revert; the copula captures tail dependence
- rules: monthly cycle: 3-week formation (Engle-Granger or KSS cointegration plus Kendall tau on hourly closes, 20 coins vs a BTC reference), 1-week trading; open when the copula conditional probability < 0.10 or > 0.90; close at the mean; 0.04% taker fee
- data needed: hourly klines: Binance public
- evidence: Binance hourly Jan-2021 to Jan-2023: 37.1% ann., vol 38.2%, Sharpe 0.97, max DD -35.6%, 176 trades, costs 11.7% of gross; Sharpe 0.48 at alpha 0.20; BTC buy-and-hold Sharpe -0.23 (Tadi-Witzany, https://arxiv.org/abs/2305.06961)
- grade: Working-paper
- risks: 2-year mostly in-sample window; cointegration breaks on delistings and narratives; -35% DD is large for "neutral"
- fit: Med — Binance-native and hourly, but needs both legs, so only once longs exist

### market_making_rebates — Passive market making and maker rebates
- direction: neutral
- family: market-making
- mechanism: takers pay the spread; the market maker is paid for inventory risk and immediacy, minus adverse selection from informed flow, plus rebates
- rules: proprietary: sub-ms infra, VIP/MM fee tiers, inventory skew, kill switches on volatility spikes (every MM pulled quotes on 10 Oct). Retail variant: post-only quoting around mid with inventory skew
- data needed: L2 depth and trades: public; rebates need VIP/LP programmes
- evidence: Wintermute revenue $1bn / profit $582m (2021), $225m revenue in 9 months of 2022 (https://www.theblock.co/amp/post/196559/wintermute-sees-revenue-plunge-but-ceo-remains-optimistic-forbes); ADV $15bn (2025) -> $10bn (2026), $1bn over 5 years into AI/HFT, non-crypto revenue target > 50% by 2027 (https://coinmarketcap.com/academy/article/wintermute-plans-dollar1b-push-into-ai-and-traditional-finance-over-5-years); GSR FY-Jun-2024 revenue $287m, profit $71m; Jane Street $39.6bn 2025 revenue. 15m cross-sectional reversal: gross Sharpe 7-13 but costs 5-10x the edge at retail tiers
- grade: Practitioner-data
- risks: adverse selection, -75% peak-to-trough revenue cycles, hacks, venue ADL, latency race
- fit: Low — closed to a taker-fee desk; only "be the patient limit order" (maker entries) is borrowable

### range_grid_neutral — Range grid on low-trend perps
- direction: neutral
- family: market-making
- mechanism: harvests mean reversion inside a range; implicitly short volatility
- rules: *(proposed)* only when ADX(1h) < 20 and the 7d range < 2x ATR(1d); 6-10 post-only levels, 1-2x leverage; kill the grid on a 1h close outside the range +- 0.5 ATR
- data needed: klines: Binance public; post-only order path
- evidence: no independent stats; exchange-promoted
- grade: Anecdote
- risks: one trend break wipes out months of grid income; fees; funding on the inventory
- fit: Low — conflicts with the event-driven desk and the guardian's design

### short_vol_options — Selling crypto implied volatility (variance risk premium)
- direction: neutral
- family: volatility
- mechanism: option buyers (retail upside calls, structured products, crash puts) overpay; implied > realised most of the time
- rules: sell delta-hedged 7-30 day 25-delta strangles or straddles on Deribit/Binance options; hedge hourly with perps
- data needed: options IV (Deribit public, Binance eapi): not USDⓈ-M
- evidence: Sygnum lists options among its instruments; carry and implied skew co-move, so crash risk is priced (BIS WP 1087); VRP size not verified this run
- grade: Single-source
- risks: crypto jumps are frequent and large (10 Oct gaps); margin spirals; thin Binance options
- fit: Low — outside the perp toolset; tail risk mismatches a small account

### venue_dislocation_fade — Venue-specific dislocations in stress (depegs, collateral pricing)
- direction: both
- family: event/supply
- mechanism: on 10 Oct 2025 Binance priced USDe/BNSOL/WBETH collateral off its own order book; USDe hit $0.62-0.65 on Binance only while ~$1 elsewhere, liquidating accounts that used it as margin; buyers of the local dislocation were paid by forced sellers
- rules: buy a local depeg only if a redemption or cross-venue path exists; exit at peg +-1%; time stop 24h
- data needed: Binance spot books for the collateral asset, external prices
- evidence: Binance paid ~$283m compensation plus a $400m user programme (https://www.coingecko.com/learn/october-10-crypto-crash-explained, https://insights4vc.substack.com/p/inside-the-19b-flash-crash)
- grade: Practitioner-data
- risks: one-off; Binance changed collateral pricing afterwards; real depegs don't come back
- fit: Low — as a strategy; as a RULE it is High: margin only in USDT/USDC, never yield-bearing or synthetic collateral

### forecast_tilted_delta_neutral — Prediction-enhanced delta-neutral
- direction: neutral
- family: other
- mechanism: a carry/basis core plus direction and volatility forecasts that tilt exposure
- rules: not published
- data needed: Binance public data plus model features
- evidence: Yohalpha reports core delta-neutral ~15-20% ann., enhanced ~30%, max DD < 0.75% (https://blog.1token.tech/tradfi-perpetual-arbitrage/); Moon Pursuit (neutral core plus directional overlays) +52% 2025 YTD
- grade: Single-source
- risks: unverified manager claims; the tilt adds hidden directional risk
- fit: Low — needs a carry core first; the tilt is what shortbot already does

### adr_local_perp_triangle — ADR / local share / perp mispricing
- direction: neutral
- family: stat-arb
- mechanism: one company trades as a local share, a US ADR and a crypto perp referencing one of them; hours, FX and liquidity differ, so gaps open
- rules: trade the gap between the perp and the listing its index tracks
- data needed: perp data on Binance; ADR/local prices external
- evidence: SK hynix ADS (1 ADS = 1/10 share) listed Jul-2026 as the example; "the spread is not free money" (https://blog.1token.tech/tradfi-perpetual-arbitrage/)
- grade: Anecdote
- risks: settlement delays, funding bleed while waiting, FX
- fit: Low — needs broker access and multi-market hours

### expiry_index_pressure — Pushing a thin reference price around an expiry (DO NOT TRADE)
- direction: neutral
- family: other
- mechanism: SEBI alleged Jane Street bought Bank Nifty constituents and futures early on expiry days while holding larger short index-option exposure, then sold; crypto analogues would be moving index constituents near settlement or funding snapshots
- rules: none: listed only as a prohibited pattern. Veto: no trade whose P&L depends on our own price impact
- data needed: n/a
- evidence: SEBI interim order Jul-2025, Rs 36,000 cr profit cited, $560-567m escrow, trading reinstated 21 Jul 2025; Jane Street calls it index arbitrage (https://en.wikipedia.org/wiki/Jane_Street_Capital, contested)
- grade: Single-source
- risks: market-abuse enforcement, account bans
- fit: Low — never

### peer_divergence_reversal — Peer-portfolio return-divergence reversal
- direction: neutral
- family: stat-arb
- mechanism: a coin that lags its most-correlated peers by more than its beta implies tends to revert as the idiosyncratic liquidity or attention shock fades
- rules: formation on 90 days of daily returns, perps with ADV >= $20M; peers = the top 20 by Pearson correlation; beta = OLS of the coin on the equal-weight peer return; weekly D = beta x peer return - own return over the last week; long the top decile of D (laggards), short the bottom decile, equal notional, dollar-neutral, one-week hold; net of fees and funding; per-name squeeze cap on the short leg
- data needed: daily klines and funding: Binance public
- evidence: Chen, Chen, Chen and Li (Management Science 2019), US equities only; implemented as ArbitrageLab's PearsonStrategy (learning-repos/arbitragelab, distance_approach/pearson_distance_approach.py, BSD-3). No crypto test (repo study 2026-10-04, docs/QUANT_METHODS.md 'What the repos taught')
- grade: Peer-reviewed
- risks: weekly crypto cross-sections may trend rather than revert (conflicts with cs_momentum_large_caps); the short leg holds pumped leaders (our squeeze tail); funding drag on shorts in alt-up regimes; delistings
- fit: Med — dollar-neutral sidesteps the alt-up drift, but needs the squeeze cap and the BTC trend gate

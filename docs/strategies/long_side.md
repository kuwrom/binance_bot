# Long-side strategies (library, research 2026-10-01)

The desk is short-only today. These cards are kept so that a long book can start from evidence if it is enabled later.
Format and status rules: [README.md](README.md). Cards marked *(proposed)* have a mechanism but no published stats:
lab-test them on stored 1h data before any use.

**Key finding:** in academic cross-sectional sorts the profit is mostly in the long leg. The CTREND low quintile earns
+0.12%/wk (t=0.16); the post-2020 momentum loser quintile earns +0.1%/wk (t=0.08). Factor rankings are therefore a long
tool. A short book needs events, flows and crowding instead ([trend_factors.md](trend_factors.md)).

### long_liq_flush_reclaim_long — Long-liquidation flush, funding flip, reclaim long (majors)
- direction: long
- family: microstructure
- mechanism: liquidated longs are forced sellers and push price below fair value; once OI is flushed and funding turns negative, new shorts are late and the first reclaim of the breakdown level forces them to cover
- rules: *(proposed)* BTC/ETH/SOL; trigger = 24h long liquidations in the top decile, or OI -8-10% in 24h with price -6% or worse; funding <= 0 at the next print; enter on the 1h close back above the cascade's breakdown level; stop under the wick low; target a 50-100% retrace of the cascade leg; time stop 72h; regime filter: price above the 200DMA, or a reclaim
- data needed: OI hist (/futures/data/openInterestHist, 30d window), funding, klines: Binance public; liquidations only live (!forceOrder@arr), no REST history
- evidence: Feb-2026: funding -6% ann. near the ~$60k bottom; on 28-Feb $420M of $500M liquidations were longs while OI rose with negative funding (https://www.coindesk.com/markets/2026/02/28/bitcoin-sets-up-potential-short-squeeze-as-funding-plunges-to-6). Glassnode wk46-2025: OI fell with price, funding at cycle lows, but ETF outflows meant "no active dip-buy signal" (https://research.glassnode.com/the-week-onchain-week-46-2025/). own 60-day backtest 2026-07-27 to 09-29, docs/BACKTEST.md "Long sleeve": 6% drop / all coins: 656 trades, +0.18R, but the matched same-hour placebo earned +0.19R (paired -0.01, t 0.1); liquid-only (>= $100M) was worse: paired -0.24 / -0.32R (t -2.0 / -2.2). Fails
- grade: Anecdote
- risks: catching knives when the trend breaks (10 Oct 2025, Nov 2025); the OI flush can repeat; ADL on alts
- fit: High — every input except liquidation history is already in the DataHub; the natural first long setup to paper-trade

### oversold_bounce_majors — Big-down-day bounce in BTC/ETH/SOL
- direction: long
- family: volatility
- mechanism: forced deleveraging overshoots in the most liquid names; dip buyers and short covering retrace part of the move within 1-3 days when the drop was driven by positioning, not news
- rules: *(proposed)* trigger = daily return < -2.5 sigma (about BTC -7%, SOL -12%), funding <= 0 at the next print, and OI down >= 8% on the day; enter on the first 1h higher low after 00:00 UTC; stop = day low - 0.5 ATR; target a 38-50% retrace; time stop 72h; skip hack, delisting or regulation drops
- data needed: klines, funding, OI hist: Binance public
- evidence: no published stats found. own 60-day backtest 2026-07-27 to 09-29, docs/BACKTEST.md "Long sleeve": 7% drop: 819 trades, -0.06R, below both placebos (paired -0.03, t -0.8); 12% drop: -0.07R (paired -0.10, t -1.8). Fails
- grade: Anecdote
- risks: trend-day continuation (10 Oct 2025, Nov 2025); repeated failed bounces in bear markets
- fit: High — data in hand and hourly-native; the simplest long counterpart to bounce_fade (same detector, other side)

### negative_funding_streak_long — Long negative-funding streak on BTC (capitulation long)
- direction: long
- family: carry
- mechanism: weeks of negative funding mean shorts have become the crowded side after a crash; spot sellers are exhausted, the reclaim forces covering, and longs are paid while waiting
- rules: trigger when Binance BTCUSDT 30d average funding is < 0 for >= 30-40 consecutive days; scale in after a reclaim (price > 20d high or > 20DMA); hold 30-60 days; stop below the capitulation low; majors only
- data needed: funding history: Binance public
- evidence: **Own test** (§4): 3 events (2020-04, 2021-08, 2026-04); 30d +16.9% vs +3.4% unconditional, 60d +9.6% vs +7.4%. Still an anecdote at n=3. Practitioner: Jun-Jul 2021 (~40d negative, $29k -> $48k in 60d, +65%); Nov-Dec 2022 FTX (~50d negative, $15.5k -> $23k, +48%); Mar-Apr 2026 46d streak (https://phemex.com/blogs/bitcoin-funding-rates-negative-46-days-ftx-bottom). ">10 days negative -> 74% rallies, median +18.6%" (https://cryptorank.io/news/feed/fab7b-bitcoin-negative-funding-rate-rebound, no method)
- grade: Anecdote
- risks: n=2; 2018-style grinding bears had negative funding without rallies; wide stop and small size required
- fit: Med — rare (once every 1-2 years) but free to monitor; doubles as a regime flag that vetoes shorts on majors

### funding_negative_zscore_long — Negative funding z-score dip long (BTC)
- direction: long
- family: carry
- mechanism: crowded shorts pay longs; deeply negative funding z means positioning is extreme and any rise forces covering
- rules: enter when the 30d rolling z-score of daily average funding crosses below -1.5; exit when z returns above 0 or after 30 days (average hold 5 days). Variant: 7d average funding < -0.01%/8h, hold 30d
- data needed: fundingRate history: Binance public
- evidence: **Own test** (docs/REGIME_STUDIES.md §4, BTC 2020-2026): 89 trades averaging 5 days, +1.94% vs +0.57% for any 5-day hold, so +1.37% excess per trade (excess t ≈ 1.65). Practitioner: 93 trades Sep-2019 to Jun-2026, win 53.8%, +1.14%/trade, "after fees probably zero or negative" (https://tradingstrategies.work/blog/funding-rate-signal-btc-backtest). 7d avg < -0.01% -> median 30d forward +12.4%, 68.7% positive (cryptorank, no method) **Own test on alts** (§5): the same rule on top-50 perps: 4,338 trades, +0.15% excess (t 0.57): null outside BTC.
- grade: Single-source
- risks: standalone edge is about zero after costs; in bear trends funding stays negative for weeks
- fit: Med — use as a short veto and a confirmation for other long setups, not as a trigger

### high_funding_trend_confirm — High BTC funding means trend-on (guard, not a fade)
- direction: long
- family: trend
- mechanism: on BTC, high funding marks persistent leveraged bull demand, not exhaustion; fading it on majors lost money
- rules: BTC entries made while funding z > +1.5 had higher 30d forward returns: +6.95% (win 62.3%) vs +4.23% (54.1%) baseline; z > +2.5: +9.87% (64.5%). Rules: (1) never short on funding alone (any class); (2) aggregate gate: when BTC/ETH 7-day carry is in the top quintile of its trailing year (roughly > 20-30% annualised), cut the size of broad/beta alt shorts; setup-level fades need no change
- data needed: funding history: Binance public
- evidence: BTC 2019-2026 backtest (https://tradingstrategies.work/blog/funding-rate-signal-btc-backtest); consistent with crypto time-series momentum (Liu-Tsyvinski RFS 2021). Own study on Binance 2020-07 to 2026-08 (own study, docs/REGIME_STUDIES.md): top-quintile BTC/ETH carry -> alt basket short 7d -4.3% vs +0.1% on other days (diff -4.4%, Newey-West t -3.1, same sign 2020/2021/2023/2024, opposite 2026 YTD), 30d alt crash frequency 12.5% vs 24.0%; top-gainer fades unaffected; coin-level funding has no edge (36 cells). Contradicts BIS WP 1087 on this venue
- grade: Practitioner-data
- risks: 35 regime cells were tested, and t -3.1 sits near the Bonferroni bar (~3.2); 2026 YTD reversed; the effect is market beta, not alpha
- fit: High — a free daily sizing gate for broad alt shorts, and the evidence behind "never fade on funding alone"

### dislocation_capture — Liquidation-cascade dislocation capture (perp discount, rebound)
- direction: both
- family: microstructure
- mechanism: forced liquidations and ADL push perps below spot and below fair value in crash windows; books with dry powder buy the overshoot (and sell the rich leg)
- rules: *(proposed)* after a >= 3 sigma hourly drop with a liquidation spike and a funding flip: take profit on shorts, and optionally a small long for 1-24h; mirror after a short-squeeze exhaustion. Full relative-value version buys the perp discount against spot. Veto for the short book: no NEW shorts right after a long-liquidation cascade (the contrarian side loses during the unwind and profits after it)
- data needed: premiumIndex, !forceOrder@arr (live only), OI, depth: Binance public
- evidence: Oct 2025 (the 10-Oct cascade month): every 1Token long-short team was positive (min +18.9% ann., mean +49.8%), and some funding-arb teams earned more from basis trading than from funding (https://blog.1token.tech/crypto-quant-strategy-index-vii-oct-2025/). Hyperliquid HLP made +$40-41.5m (~+10%) on the 10-11 Oct weekend (https://www.coingecko.com/learn/hyperliquid-hlp-vault-analysis). Aug-2007 quant quake: contrarian liquidity provision lost during the coordinated unwind and profited after it (Khandani-Lo, https://www.nber.org/papers/w14465). No peer-reviewed crypto rebound estimate
- grade: Practitioner-data
- risks: catching knives; outages and API lockouts in the window; ADL on the profitable leg
- fit: Med — the exit-rule use (take profit into cascades) is low-risk now; the long use needs a long book

### capitulation_forced_selling_long — Buy a coin-specific forced-selling capitulation, time-boxed
- direction: long
- family: event/supply
- mechanism: forced sellers (liquidations, insolvent holders such as Celsius) dump regardless of price; liquidity returns slowly and price retraces
- rules: trigger when a coin drops ~40% and breaks its prior range for an identifiable forced-selling reason; investigate the cause first, then enter; time exit 2-3 days up to 1-2 weeks ("don't hold on too long"); stop not published. Avoid breakout-chasing
- data needed: klines, OI hist (30d), news: mostly Binance public
- evidence: Selini Capital CIO (13 years profitable, self-reported): GMT -45% retraced in 2-3 days; after alts are -80-90% it is "hard to find a lot of shorts"; shorting alts was his "big flaw for many years" (https://blockcrunch.substack.com/p/how-this-top-trader-is-navigating, https://panews.io/articles/0rf24oeo). own 60-day backtest 2026-07-27 to 09-29, docs/BACKTEST.md "Long sleeve" (mechanical version, no cause check): -30%/-40% over 7 days then a 24h-high breakout: 59-134 trades, -0.03 to +0.09R, every variant below its same-hour placebo (paired -0.11 to -0.20). Fails mechanically; the card's "identifiable forced-selling reason" is untested **Own test** (§5, daily, top-50 perps): a 7-day drop <= -30% and >= 20 pp below the market: 3/7/14-day excess -0.9% / -1.1% / -1.9% (t ~ -0.9); in uptrends (BTC 60d > 0) -2.8% / -2.1% / -6.4% (t -2.1 / -1.1 / -2.5). Rejected: crashed coins keep falling.
- grade: Anecdote
- risks: catching a real collapse (LUNA, FTT); 10-Oct wicks to ~0 then a partial recovery; venue lockout during the event
- fit: Med — the natural exit-and-flip for our short book after cascades, and the first long sleeve to add

### premium_index_reversion — Perp premium (mark - index) mean reversion
- direction: both
- family: stat-arb
- mechanism: the perp is tied to the index only through funding and arbitrage capital; one-sided leveraged flow overshoots the premium, and arbitrageurs pull it back
- rules: *(proposed)* 1h premiumIndexKlines, z-score vs 7d; z > +3 at a 24h high -> short (our premium_hi); z < -3 after a dump -> LONG the perp for 4-24h; stop on a new low in the premium
- data needed: /fapi/v1/premiumIndexKlines, markPriceKlines, indexPriceKlines: Binance public
- evidence: perp deviations are large, co-move across coins and decline over time (He-Manela-Ross-von Wachter, https://arxiv.org/abs/2212.06888); premia are sticky but mean-reverting (https://robotwealth.com/the-art-and-science-of-trading-carry/); funding drives most realised basis returns (https://arxiv.org/abs/2605.05089). own 60-day backtest 2026-07-27 to 09-29, docs/BACKTEST.md "Long sleeve", long side: z <= -3: 4,174 trades, +0.01R (gross +0.10R), +0.03R over the matched placebo (t 2.6, IS -0.00 / OOS +0.05) but stress -0.08R and DSR 0.00: "promising", the only long variant worth shadow-logging; z <= -4: +0.03R paired (t 1.6) **Repo study 2026-10-04 (negative, out of sample):** Jansen's Binance-perps case study ranked 19 perps every 8h on premium, funding and volatility. The chosen setup (1 of 2,807 candidates) had a validation Sharpe of 1.67 (PSR p 0.005), but a Deflated Sharpe below 0 under every trial count, then a 2024-25 holdout Sharpe of -0.83 (CAGR -45%, max DD -82%); execution costs ran more than 4x the funding earned (learning-repos/machine-learning-for-trading/case_studies/crypto_perps_funding (Jansen, MIT)). Data quirk from the same study: the premium index is exactly 0 whenever the index sits inside the impact bid/ask (much more often for illiquid coins), so a 0 means 'inside the spread', not missing.
- grade: Working-paper
- risks: deep discounts accompany real bad news (hacks, delistings); the discount can widen further in cascades
- fit: Med — the long mirror of premium_hi; same data and code path

### cycle_low_accumulation — Cycle-low BTC accumulation (slow overlay)
- direction: long
- family: other
- mechanism: capitulation (forced sellers, negative funding, ETF outflows) pushes BTC below fair value; patient capital is paid by forced and fearful sellers
- rules: Moon Pursuit runs "bitcoin accumulation at cycle lows" (rules not published). Desk default *(proposed)*: scale in when BTC is >20% under the 200DMA, 30d funding average < 0 and drawdown from ATH > 40%; or (Glassnode) below the short-term-holder cost basis -1 SD band once STH realised losses roll over; hold months; low leverage
- data needed: klines, funding: Binance public; STH bands need Glassnode (paid); proxy = price vs 155d VWAP
- evidence: **Own test** (§4, mechanical rule): 3 events (2021-07, 2022-11, 2026-02); 90d +35.8% vs +12.6% unconditional, 180d +36.3% vs +31.3%. n=3, an anecdote. Moon Pursuit Fund I +52% 2025 YTD, ~+170% since Apr-2024 (overlay attribution unknown) (https://www.coindesk.com/business/2025/12/17/moon-pursuit-capital-launches-usd100-million-market-neutral-crypto-fund). Nov-2025: price $89k below the STH -1 SD band ($95.4k), STH realised losses $523M/day, equal to FTX (https://research.glassnode.com/the-week-onchain-week-46-2025/)
- grade: Single-source
- risks: few signals per decade; -50% after "cycle low" calls (2022); months below the band in true bears
- fit: Low — rare and long-horizon; at most a regime note ("stop shorting majors when this fires")

### usdt_exchange_inflow_long — Stablecoin exchange inflows, long BTC/ETH
- direction: long
- family: event/supply
- mechanism: stablecoins arriving on exchanges are dry powder that precedes spot buying
- rules: hourly z-score of USDT net exchange inflow (1-6h windows); long BTC/ETH perp when inflow is top-decile; hold 1-6h; large treasury mints as a daily trigger
- data needed: on-chain exchange flows (CryptoQuant/Glassnode/Arkham): NOT Binance
- evidence: 2017-2023, USDT net inflows positively predict BTC and ETH returns at 1-6h (https://arxiv.org/abs/2411.06327); Tether issuance timed after downturns lifted BTC in 2017 (Griffin-Shams, JF 2020, https://doi.org/10.1111/jofi.12903)
- grade: Working-paper
- risks: paid data; flows now dominated by ETF/OTC and yield stablecoins; noisy wallet labels
- fit: Med — strong mechanism for a future long book; needs a paid on-chain feed

### etf_flow_follow — Spot-ETF flow follow (BTC/ETH/SOL)
- direction: long
- family: other
- mechanism: ETF creations are price-insensitive spot demand; inflow streaks mark institutional allocation, outflow streaks remove the marginal bid
- rules: *(proposed)* 3-day net inflow > +$500M and price above the 20DMA -> allow longs; 3-day outflow < -$500M -> veto longs and favour shorts on high-beta names; act at the 00:00 UTC bar (flows post after the US close)
- data needed: Farside/SoSoValue flows (free web, not Binance)
- evidence: Glassnode Nov-2025: "deeply negative" ETF flows were the main obstacle to a bottom (https://research.glassnode.com/the-week-onchain-week-46-2025/); no lead-lag stats found
- grade: Anecdote
- risks: flows are partly hedged basis trades; published with a lag; mostly contemporaneous with price
- fit: Low — a regime input only

### korean_listing_announce_scalp — Upbit/Bithumb listing announcement scalp
- direction: long
- family: event/supply
- mechanism: Korean retail demand hits a token that already trades on Binance perps; the announcement reprices it 30-100%+ within minutes
- rules: only with the notice caught in < 5-10 s (Upbit notice API); market buy, 1-3% risk; exit within minutes to hours; never hold overnight
- data needed: Upbit/Bithumb notice feeds (public, not Binance), Binance klines/OI
- evidence: intraday moves of 40-800%+ (vendor claim, https://paragraph.com/@pkcryptolisting/how-to-capture-upbit-and-bithumb-listing-alpha-before-everyone-else-2026). 2025 listings held from day-1 close to 11-Mar-2026: Upbit n=59 avg -69.5% (median -80.9%) (https://www.odaily.news/en/post/5209720). Upbit abnormal listing return up to +8.9% (BRL 2019 working paper)
- grade: Anecdote
- risks: a latency race an hourly bot loses; the pump reverses within hours
- fit: Low — as a long; High as a SHORT trigger 6-48h after the pump stalls (see airdrop_hodler_supply_short)

### buyback_burn_long — Protocol buyback/burn programme long
- direction: long
- family: event/supply
- mechanism: programmatic, price-insensitive buying from protocol revenue creates a persistent bid that lags the news
- rules: *(proposed)* new or enlarged buyback >= 2% of market cap per year; wait for the 24h pop to settle; buy the pullback to the announcement-day VWAP; hold 2-6 weeks BTC-beta-hedged; exit on the first weekly close below the 20DMA. Separate revenue-funded buybacks from treasury theatre (our insider_buyback_pump is the short side)
- data needed: news/buyback trackers (external), klines (public)
- evidence: none quantified
- grade: Anecdote
- risks: buyback theatre without revenue; insiders distribute into the bid
- fit: Low — infrequent and slow

### dominance_rotation_long — BTC-dominance-down alt rotation
- direction: long
- family: cross-sectional
- mechanism: after BTC trends, profit rotates into ETH, then large caps, then small caps; ETH/BTC breakouts lead
- rules: *(proposed)* ETH/BTC above its 50DMA and BTC lagging a top-20 perp basket over 14d -> allow longs in the top-5 alts by 14d residual strength; off on an ETH/BTC 50DMA loss
- data needed: klines (public); dominance itself is external
- evidence: **Own test** (§4, weekly 2020-2026): the dominance regime adds nothing (top-5 residual-strength alts +1.77%/wk on-regime vs +2.10% off). The residual-strength pick beats the top-20 basket by +1.1-1.3%/wk in BOTH regimes (residual momentum)
- grade: Practitioner-data
- risks: alt seasons are rare and short after 2021; mostly contemporaneous
- fit: Low — the regime filter failed; the residual-strength ranking is the useful part (see beta_hedged_rs_pair)

### attention_shock_long — Investor-attention surge long
- direction: long
- family: other
- mechanism: limited attention: retail inflows follow search/social attention with a lag
- rules: weekly abnormal Google-search or social-mention growth vs a 4-week mean; long top-attention coins with a positive trend; hold 1-4 weeks
- data needed: Google Trends/social APIs (not Binance)
- evidence: "proxies for investor attention strongly forecast future cryptocurrency returns" (BTC/ETH/XRP 2011-2018, Liu-Tsyvinski RFS 2021, https://doi.org/10.1093/rfs/hhaa113); retail momentum behaviour (Kogan et al. JFE 2024, https://doi.org/10.1016/j.jfineco.2024.103897)
- grade: Peer-reviewed
- risks: attention peaks coincide with alt tops (MAX/lottery); pre-2019 evidence
- fit: Low — data outside Binance; context only

### adoption_growth_long — On-chain adoption growth (network factor)
- direction: long
- family: cross-sectional
- mechanism: network externalities: user growth raises token value
- rules: rank L1/L2 tokens weekly or monthly on growth of active/new addresses; long the top group. Value variant (CVALUE): long low market-cap-to-new-addresses, avoid high
- data needed: on-chain address data (Glassnode/Coin Metrics), market cap: not Binance
- evidence: adoption explains ~8% of return variation; CVALUE 5-1 = -2.3%/wk post-2020 (t=-2.32), i.e. cheap coins 3.0%/wk vs expensive 0.7%/wk (Borri-Liu-Tsyvinski-Wu 2026, https://arxiv.org/abs/2510.14435); value decile 6.8%/wk (t=3.00) in >4,000 assets (Cong-Karolyi-Tang-Zhao, https://ssrn.com/abstract=3985631)
- grade: Peer-reviewed
- risks: address counts are gameable (airdrop farming); slow signal; the expensive leg is flat, not negative
- fit: Low — data and horizon mismatch for an hourly perp desk

### monday_btc_effect — Monday BTC returns
- direction: long
- family: other
- mechanism: weekend illiquidity followed by Monday institutional/CME flow
- rules: long BTC Monday 00:00-24:00 UTC; or just avoid opening shorts into Monday
- data needed: daily klines: Binance public
- evidence: only BTC shows significantly higher Monday returns, and the trading simulation was "not significantly different from random" (Caporale-Plastun FRL 2019, https://doi.org/10.1016/j.frl.2018.11.012)
- grade: Peer-reviewed
- risks: pre-2018 sample; probably gone in the ETF era
- fit: Low — a calendar prior only

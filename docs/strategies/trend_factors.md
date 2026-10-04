# Trend and cross-sectional factor strategies (library, research 2026-10-01)

Format and status rules: [README.md](README.md).

**Read before trusting any premium:**
- Published crypto anomalies concentrate in small, illiquid coins and shrink under cost and size filters (Fieberg et al. IRFA 2024).
- Short legs are weak. The CTREND low quintile earns +0.12%/wk (t=0.16), the post-2020 momentum loser quintile +0.1%/wk (t=0.08) and the CVALUE expensive quintile +0.7%/wk. Factor sorts tell a short book what NOT to short; absolute short alpha needs events, flows or crowding.
- Net of realistic costs, Binance-perp cross-sectional momentum 2020-2026 is about zero (gross +0.573%/wk vs 0.40 pp/wk costs, SSRN 7404139).
- Pro directional teams (1Token, 4 teams, 2026) won ~40% of trades with a payoff of 0.8-1.0 in losing months, and lost in 4 of 5 months after a +112% ann. January. Trend is lumpy.

### tsmom_trend_following — Time-series trend on BTC, ETH and large alts
- direction: both
- family: trend
- mechanism: slow diffusion of adoption news, retail momentum trading and reflexive leverage make returns autocorrelated over days to weeks; late entrants and forced deleveragers pay the trend follower
- rules: sign of the trailing 1-4 week return (or price vs 50/200 EMA, or a 20/60/120-day return blend) -> long/flat/short the next week; volatility-target each asset (~1% daily risk); 3x ATR(14) trailing stop; rebalance daily or weekly; enter on the first 1h close after the weekly signal flips. Practitioner variant: 6h trend signals with vol-calibrated trailing stops and a 70/30 long/short capital split. Short-book veto *(proposed)*: short only if close < 20d and 60d EMA and 30d return < 0. Robot Wealth "YOLO": trend plus momentum basket with inverse-volatility weights, updated daily (lookbacks members-only)
- data needed: klines: Binance public
- evidence: trend profitable for 110+ years across futures asset classes (Hurst-Ooi-Pedersen 2017, https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing); "strong time-series momentum effect" in crypto (Liu-Tsyvinski RFS 2021, https://doi.org/10.1093/rfs/hhaa113); Winton +7.4% in 2025 vs multi-strats 10-28% (https://www.fortune.com/2026/01/02/top-hedge-fund-performers-2025-bridgewater-de-shaw-citadel-millennium); retail crypto traders are momentum traders (Kogan et al. JFE 2024). 150+ pairs OOS 2022-2024 Sharpe 2.41, max DD -12.7% (https://arxiv.org/abs/2602.11708, likely optimistic). Galaxy VisionTrack Quant index +105.8% (2021), -26.6% (2022), +55.9%, +60.8%, -7.5% (2025), -4.2% (2026 YTD Aug) (https://visiontrack.galaxy.com/indices/). 1Token directional teams: Jan-2026 +112% ann., then Feb-May -36% to -119% ann., monthly max DD 7.8-11.9% (worst 17.9%) (https://blog.1token.tech/crypto-quant-strategy-index-xiii-may-2026/) **Own test** (docs/REGIME_STUDIES.md §5, 2020-2026): gating long BTC on its 60d return keeps the mean (+0.108%/day vs +0.115%) at 55% exposure, Sharpe 0.69 -> 0.93, max DD -79% -> -61%; on the top-30 alt basket Sharpe 0.44 -> 0.63, DD -92% -> -74%. Next-day return gate-on +0.20% vs off +0.01% (t 1.4): mostly a risk cut. Used as gate G for every long (docs/LONG_PLAYBOOK.md). **Repo study 2026-10-04:** Carver's live configuration uses a ladder of six EWMAC speeds plus volatility-normalised breakout and momentum as one forecast family, each scaled to average |F| = 10 and combined with a diversification multiplier, not a single lookback (learning-repos/pysystemtrade (Carver, GPL-3: formulas reimplemented, never copied), repo study 2026-10-04).
- grade: Peer-reviewed
- risks: whipsaw in ranges (2025-26 negative); gaps (10 Oct: -14% in an hour); crowded CTA exits; many "quant" funds carry hidden long beta (-27% in 2022)
- fit: High — public data; for the short book it is a regime filter ("only short coins in a confirmed downtrend"), for a long book the core strategy

### ctrend_technical_aggregate — CTREND: elastic-net aggregate of 28 technical signals
- direction: long
- family: trend
- mechanism: crypto has no cash-flow fundamentals, so price-path information (MA distances, oscillators, volume) is what gets priced, slowly
- rules: weekly cross-sectional regressions of next-week return on 28 daily indicators (SMA 3-200d distances, MACD 12/26, RSI, stochastic, Bollinger, volume MAs), combined by elastic net; quintile sort; long the top quintile (short the bottom for relative value); hold 1-4 weeks
- data needed: daily OHLCV: Binance klines
- evidence: **Own replication** on Binance top-100 perps, 2021-03 to 2026-08 (docs/REGIME_STUDIES.md §3: 21 indicators, ridge refit weekly out of sample): long-short −0.07%/wk gross (t −0.36), −0.27% net (t −1.44); both legs track the universe. **Does not replicate on our tradable universe.** Paper: >3,000 coins Apr-2015 to May-2022: high-low 3.87%/wk (t=5.19) gross, 2.90%/wk (t=3.89) net of 40/30 bp, 2.35% net of 60/50 bp; top-100 coins 1.90%/wk net worst case (t=2.50); breakeven cost 1.41%; weekly turnover 68%; robust across 55,296 design variants; subsumes momentum. Low quintile +0.12%/wk (t=0.16): the short leg is flat (Fieberg et al. JFQA 2025, https://doi.org/10.1017/S0022109024000747) **Repo study 2026-10-04:** on Qlib's Alpha158 benchmark (CSI300 equities) a linear model reaches ~88% of LightGBM's IC (0.040 vs 0.045), which supports a ridge baseline with boosting only as a challenger (learning-repos/qlib, examples/benchmarks).
- grade: Peer-reviewed
- risks: sample ends May 2022; look-ahead if refit carelessly; the short leg is useless in absolute terms
- fit: Low — failed our out-of-sample replication on liquid perps (§3); the paper's premium appears to live in coins we cannot trade

### cs_momentum_large_caps — Cross-sectional 1-4 week momentum
- direction: both
- family: cross-sectional
- mechanism: under-reaction and attention-driven continuation, strongest in large liquid coins (behavioural, not liquidity)
- rules: weekly quintile sort on the past 1-4 week return; long the top, short the bottom quintile (the long leg carries it); hold 1 week; value-weighted. Lower cost: hold longer than a week and trade fewer names
- data needed: daily klines; market caps (CoinGecko) or quoteVolume as a proxy
- evidence: 1,707 coins 2014-2018: long-short 2.5-4.1%/wk; above-median size 4.2%/wk vs 0.6% below (Liu-Tsyvinski-Wu JF 2022, https://doi.org/10.1111/jofi.13119). Post-2020 replication to Sep-2025: 2.1%/wk (NW t=3.70) but the loser quintile +0.1%/wk (t=0.08) (https://arxiv.org/abs/2510.14435). NEGATIVE: 832 Binance perps incl. delisted, 2020-2026: gross +0.573%/wk vs costs 0.40 pp/wk, net ~0 (https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7404139); lookback sweep 7d +33%, 10d -23%, 14d +97%, 21d +35%: unstable (https://dev.to/pavel_kkkkazantsev/three-crypto-edges-three-different-killers-1i6o) **Repo study 2026-10-04:** Carver's relmomentum computes the same idea on volatility-normalised cumulative returns against the class median, which matches our relative-strength-vs-BTC finding (LONG_PLAYBOOK L1) (learning-repos/pysystemtrade (Carver, GPL-3: formulas reimplemented, never copied), repo study 2026-10-04).
- grade: Peer-reviewed
- risks: costs eat it on perps; momentum crashes when alt season ends; unstable parameters
- fit: Med — not a standalone book; a rank tilt for choosing between candidates (short the weakest, long the strongest)

### dollar_neutral_factor_book — Dollar-neutral cross-sectional long-short book
- direction: neutral
- family: cross-sectional
- mechanism: harvests sector dispersion and liquidity dislocations (momentum, carry, reversal, supply) without market beta; paid by slow or forced flows into single coins
- rules: rank the top-50 perps on 2-4 week momentum, funding, last-day reversal (illiquid bucket) and an unlock/supply score; long the top, short the bottom quintile, beta-neutral to BTC (60d beta); rebalance weekly; portfolio stop: -5% in a month halves gross. Pro books hold hours to ~4 days with concentration beta 0.03-0.09
- data needed: klines, funding, OI: Binance public; unlock schedules manual
- evidence: 1Token long-short index (2-7 teams), annualised monthly means: +28.9% (Jul-2025), -58.8% (Sep), +49.8% (Oct, all teams positive), -4.1% (Nov), +40.6% (Jan-2026), -118.8% (Feb, all negative), -21.4% (Mar), +52.3% (Apr), +41.9% (May); vol 12-38% ann.; monthly max DD 3.8-12.3%, worst 18.3%; holding 0.05-3.8 days (https://blog.1token.tech/crypto-quant-strategy-index-x-feb-2026/). Bybit dollar-neutral 66.69% in 2025 (venue-sponsored). Crypto Fund Research: 117 quant funds, median Sharpe 1.53, median 2025 +3.2%, 58% positive (https://cryptofundresearch.com/quant-crypto-hedge-funds/)
- grade: Practitioner-data
- risks: extreme dispersion (-151% to +172% ann. within a month); factor crashes (Sep-2025, Feb-2026); squeezes on the short leg; alts drop 40-80% vs BTC -14% in cascades, so the beta hedge breaks
- fit: Med — the best structural use of factors once longs exist; today our setups are the short leg

### carry_momo_breakout_blend — Three-signal perp portfolio (Robot Wealth)
- direction: both
- family: cross-sectional
- mechanism: leveraged retail trend-chasing in a fragmented, hard-to-value market gives momentum and breakouts; crowded funding gives carry; blending diversifies
- rules: top-30 Binance perps by 30d volume ex-stables; weight = 0.5 carry (24h funding, cross-sectional z) + 0.2 momentum (10d return, cross-sectional) + 0.3 breakout (closeness to the 20d high, -9.5..+9.5); normalise to portfolio weights; rebalance daily only when a position drifts past a no-trade buffer; 15 bp per trade cost model; no stops (portfolio level)
- data needed: daily klines, funding: Binance public
- evidence: **Own test** on Binance top-30 perps, 2020-03 to 2026-08 (docs/REGIME_STUDIES.md §3): long-short +0.23%/day gross (t 2.17, Sharpe 0.86), **+0.16% net (t 1.55, Sharpe 0.61)**; 2022 and 2024 negative; the long leg carries everything (+0.24%/day, t 2.14) and the short leg earns nothing (−0.01%, t −0.23). Robot Wealth: Sep-2019 to Feb-2024 backtest; costs "significantly" erode returns without the buffer, the buffer restores them; Sharpe not published (https://robotwealth.com/a-simple-effective-way-to-manage-turnover-and-not-get-killed-by-costs/). The closest documented small-team perp book. Its carry sleeve failed out of sample in a separate 2025-26 test (see xs_funding_carry_basket)
- grade: Practitioner-data
- risks: long-biased in bull runs (breakout and momentum legs); crowding; post-2024 decay unknown
- fit: Med — the best systematic book found so far but not significant net, and its edge is the long leg only; the short half alone is worthless. Shadow-track it as the candidate core for a future long/neutral book

### beta_hedged_rs_pair — BTC-beta-hedged relative-strength alt long (or weak-alt short)
- direction: neutral
- family: cross-sectional
- mechanism: isolates idiosyncratic strength (narrative/flow) from market beta, so a market dump does not stop out the thesis
- rules: *(proposed)* rank the top-50 perps by 30d volume on 7-14d return residual to BTC (rolling 30d beta on 1h returns); long the top 1-3 and short beta x notional of BTC; hold 3-10 days; exit when the residual rank leaves the top 10 or on a -1.5 ATR residual stop; re-beta daily. Mirror: short the weakest alt against a BTC long
- data needed: 1h klines: Binance public
- evidence: momentum is a priced crypto factor (Liu-Tsyvinski-Wu JF 2022); but the net-of-cost Binance-perp replication is null (SSRN 7404139) **Own test** (§5, weekly 2020-2026): long the top 5 of the top-30 perps by 28d return residual to BTC for 7 days: 1,658 name-weeks, **+1.76%/week** excess vs the 30 after costs and funding (two-way t 2.40; bootstrap 95% CI [+0.57, +3.20]%); 7d/14d lookbacks +1.54%; positive in 6 of 7 years (2022 -0.1%), both halves, both trend regimes. Median -2.4%: a tail edge. The hourly RS-leader pullback entry adds nothing (-0.02R vs placebo). Playbook setup L1.
- grade: Working-paper
- risks: beta jumps to 1.5-2 in crashes; momentum crashes; two legs of fees and funding
- fit: Med — the cleanest way to add longs without naked market risk; testable in shortbot.lab now

### breakout_20d_overlay — 20-day-high breakout overlay
- direction: long
- family: trend
- mechanism: trend persistence; nearness to the 20d high predicts next-day return
- rules: feature = position of the latest 20d high (+9.5 = new high today, -9.5 = 20 days ago); scale exposure up near +9.5; combine with carry and momentum ranks; daily rebalance; exclude the bottom 20% by volume
- data needed: daily klines: Binance public
- evidence: monotonic relationship with mean next-day returns on Binance perps; no Sharpe published (https://robotwealth.com/quantifying-and-combining-crypto-alphas/) **Own test** (§5, top-50 perps 2020-2026): 2,007 breakouts, 5-day excess vs the market **+1.54%** after costs and funding (two-way t 2.18; block-bootstrap 95% CI [+0.28, +2.99]%); 10d/20d holds decay (+0.8%, t ~1), a 3xATR chandelier is worse. Median -2.2%, top 5% of trades = 270% of the total: a tail edge. Hourly version (3d/7d high on 3x volume) is WORSE than the same-hour placebo (-0.07R, t -1.4 to -2.1, BACKTEST.md long round 2). Playbook setup L2. **Repo study 2026-10-04:** Carver's continuous breakout, 40 x (P - mid_N)/(max_N - min_N) smoothed by EWMA(N/4), gives partial size inside the range instead of a binary trigger; his BTC weights keep only slow variants (160/320 days) (learning-repos/pysystemtrade (Carver, GPL-3: formulas reimplemented, never copied), repo study 2026-10-04).
- grade: Practitioner-data
- risks: false breakouts in ranges; it is market beta and needs a regime filter
- fit: Med — a filter: allow longs in strong names, block shorts on names at 20d highs

### alt_momentum_burst — Short-horizon alt momentum in strong-momentum windows
- direction: both
- family: trend
- mechanism: attention and flow cascades in alts (listings, narratives) run for hours to days until leverage and funding exhaust
- rules: Moon Pursuit runs it as a small opportunistic overlay "during periods of strong momentum" (rules not published). Desk default *(proposed)*: only when breadth says momentum-on; buy the 24h-high breakout with volume > 3x the 20d median; exit on a 4h close back in range or funding > 0.1%/8h; hold hours to 3 days
- data needed: klines, taker ratio, OI hist, funding: Binance public
- evidence: Moon Pursuit Fund I +52% 2025 YTD, Oct-2025 ~-3% while many funds lost double digits (https://www.coindesk.com/business/2025/12/17/moon-pursuit-capital-launches-usd100-million-market-neutral-crypto-fund); attribution to the overlay unknown
- grade: Single-source
- risks: the same coins become pump_fade candidates; violent reversals; thin books
- fit: Med — the short mirror is already pump_fade; the long side needs a long book

### daily_reversal_illiquid_momentum_liquid — Last-day return: reversal in illiquid coins, momentum in majors
- direction: both
- family: cross-sectional
- mechanism: uninformed flow pushes thin coins and liquidity providers get paid on the reversal; liquid majors absorb news gradually
- rules: daily sort on last-day return; in the illiquid bucket short yesterday's top gainers (long the losers); in the top-liquidity bucket trade with the sign; 1-day hold
- data needed: daily klines + quoteVolume: Binance public
- evidence: Robot Wealth: unconditional short-term reversal is a "weak effect; unlikely profitable standalone, useful in a larger system" (https://robotwealth.com/index-of-strategies/). >3,600 coins: low last-day-return coins significantly outperform high, largest coins show daily momentum (Zaremba et al. IRFA 2021, https://doi.org/10.1016/j.irfa.2021.101908); momentum only in the largest caps, reversal elsewhere, >4,000 assets (Cong-Karolyi-Tang-Zhao, https://ssrn.com/abstract=3985631)
- grade: Peer-reviewed
- risks: illiquid alts are where squeezes and costs are worst; daily turnover
- fit: High — academic support for fading 1-day spikes in small/mid-cap perps (pump_fade) and for NOT fading BTC/ETH strength

### max_lottery_short — MAX effect: short last week's biggest one-day jumpers
- direction: short
- family: cross-sectional
- mechanism: lottery-seeking speculators overpay for coins with extreme daily up-moves, which then underperform
- rules: weekly; MAX = largest daily log return in the past week; short the highest-MAX quintile (long the lowest for relative value); hold 1 week
- data needed: daily klines: Binance public
- evidence: Jan-2016 to Dec-2019: low-minus-high MAX 1.54%/wk, HAC-bootstrap t=2.68, robust to trimming and BTC risk (Grobys-Junttila JIFMIM 2021, https://doi.org/10.1016/j.intfin.2021.101289). 412 Telegram pump-and-dumps: targets are predictable ex ante (Xu-Livshits USENIX Security 2019, https://arxiv.org/abs/1811.10109). CONFLICT: "MAX momentum" finds high-MAX coins keep outperforming (https://doi.org/10.1016/j.irfa.2021.101829) **Repo study 2026-10-04:** in Jansen's 19-perp panel, high-volatility perps underperform over the next 8h (7-day vol IC -0.041; GARCH vol IC -0.039, HAC t -5.1) (learning-repos/machine-learning-for-trading/case_studies/crypto_perps_funding (Jansen, MIT)); see xs_low_vol_perps.
- grade: Peer-reviewed
- risks: conflicting evidence; squeeze risk dominates on perps; pre-2020, not cost-adjusted
- fit: High — the academic basis for pump_fade; a weekly MAX rank is a cheap prior for which pumpers to fade

### size_small_minus_big — Crypto size premium
- direction: long
- family: cross-sectional
- mechanism: illiquidity and neglect premium on micro-caps
- rules: weekly quintile sort on market cap; long the smallest, short the largest
- data needed: market cap (not Binance)
- evidence: 3.4%/wk 2014-2018 (Liu-Tsyvinski-Wu JF 2022); post-2020 -0.9%/wk (t=-1.69) (https://arxiv.org/abs/2510.14435)
- grade: Peer-reviewed
- risks: lives in coins without perps; gone in liquid universes after 2018
- fit: Low — implies only: don't short the smallest listed caps on size grounds

### intraday_tsmom_btc — BTC intraday momentum (first half-hour predicts last)
- direction: both
- family: trend
- mechanism: liquidity-provision dynamics, strongest in high-volume sessions and downturns
- rules: define the day by volume; the sign of the first 30-min return sets the position for the last 30 min
- data needed: 1-30m klines: Binance public
- evidence: positive predictability with market-timing gains, especially in BTC downturns (Shen-Urquhart-Wang Financial Review 2022, https://doi.org/10.1111/fire.12290)
- grade: Peer-reviewed
- risks: tiny per-trade edge vs taker fees; session definition fragile after the ETFs
- fit: Low — sub-hourly; a timing context only

### smooth_momentum_rsqr — Trend-quality ("frog in the pan") momentum
- direction: both
- family: cross-sectional
- mechanism: momentum built from many small moves (high R^2, low discreteness) is under-reacted to and persists; jumpy few-bar moves (pumps) attract attention and mean-revert
- rules: top 100 perps by 30-day volume; score = rank(20-day return) x rank(RSQR20), RSQR20 = R^2 of a linear fit to the last 20 daily closes; long the top quintile, short the bottom quintile, weekly rebalance; control = plain 20-day-return quintiles; report the incremental IC
- data needed: 1d klines: Binance public
- evidence: Da, Gurun and Warachka, "Frog in the Pan" (Review of Financial Studies 2014), US equities only. RSQR5/10/20/60 are among Qlib's selected Alpha158 features (learning-repos/qlib, examples/benchmarks/GRU/workflow_config_gru_Alpha158.yaml). No crypto test (repo study 2026-10-04, docs/QUANT_METHODS.md 'What the repos taught')
- grade: Peer-reviewed
- risks: alt-season beta; the short side carries the squeeze tail; low R^2 also flags the coins we already avoid
- fit: Med — cheap from data we have; RSQR is also a squeeze-risk feature for pump_fade (jumpy pumps revert, smooth ones persist)

### pv_corr_reversal — Price-volume correlation reversal
- direction: both
- family: cross-sectional
- mechanism: a move on falling volume (low or negative price-volume correlation) is unsupported and tends to reverse; a volume-confirmed move does not
- rules: daily CORD10 = correlation over 10 days of daily returns with the change in log volume, and the same with taker-buy volume; short the top 5-day-return quintile that also has the lowest CORD10, long the mirror; 3-day hold; costs and funding; matched placebo
- data needed: 1d/1h klines with taker volume: Binance public
- evidence: CORR/CORD are 7 of the 20 features Qlib/RD-Agent select (learning-repos/RD-Agent, rdagent/utils/qlib.py); price-volume correlation alphas in Kakushadze, "101 Formulaic Alphas" (2015), equities. No crypto test (repo study 2026-10-04, docs/QUANT_METHODS.md 'What the repos taught')
- grade: Working-paper
- risks: short horizon, costs dominate; volume is unreliable on thin perps (wash trading)
- fit: Med — taker-volume CORD is a native crypto upgrade

### xs_low_vol_perps — Cross-sectional low-volatility perp book
- direction: both
- family: cross-sectional
- mechanism: lottery-seeking, leveraged buyers crowd into high-volatility perps and overpay (they also carry the richest premium and funding); low-volatility perps are under-owned. The betting-against-beta analogue
- rules: top ~40 USDT perps by 30-day volume, listed >= 60 days; at each 8h settlement rank on causal GJR-GARCH(1,1) conditional volatility (weekly refit) or 7-day realized volatility; long the bottom quintile, short the top quintile, BTC-beta-neutral, each leg scaled to equal risk; hold >= 24h in overlapping tranches; costs and funding
- data needed: 1h/8h klines, funding and premium: Binance public (our stream store)
- evidence: feature evidence only: in Jansen's Binance-perps case study (19 perps, validation 2022-23) 7-day volatility had an IC of -0.041 and GARCH conditional volatility -0.039 (HAC t -5.1, significant after Benjamini-Hochberg) for the next 8h (learning-repos/machine-learning-for-trading/case_studies/crypto_perps_funding, MIT). The case study's own ML book on these features failed its 2024-25 holdout (repo study 2026-10-04, docs/QUANT_METHODS.md 'What the repos taught')
- grade: Single-source
- risks: squeezes on the high-volatility shorts (our measured tail); the low-volatility long leg is close to a BTC/ETH beta tilt; 8h turnover eats a 0.04 IC; two validation folds only
- fit: Med — uses data we have and complements max_lottery_short, but must clear our placebo, DSR and clustered-t gates at a 24h+ horizon

### squeeze_convexity_trend_hedge — Alt-index trend sleeve as a squeeze hedge for the short book
- direction: long
- family: trend
- mechanism: trend-following returns are positively skewed and pay in the sustained alt rallies that squeeze a short book, so a small breakout-long on an alt index offsets the book's negative skew at a whipsaw cost
- rules: equal-weight index of the top 20 alt perps by 30-day volume (ex BTC/ETH/stables); long when the index closes above its 20-day high, exit below its 10-day low; size = the short book's net alt beta x h, h in {0.25, 0.5}; judge the joint book on skew, CVaR 5%, max drawdown and Sharpe vs the unhedged book, daily 2020-2026, net of costs and funding
- data needed: daily klines and funding: Binance public
- evidence: trend skew theory and multi-decade futures evidence (Sepp, TrendFollowingSystems, listed in awesome-quant; self-described); consistent with our own breakout_20d_overlay test (+1.54% over 5 days, t 2.2, tail-driven) (repo study 2026-10-04, docs/QUANT_METHODS.md 'What the repos taught')
- grade: Single-source
- risks: chop bleed; lags single-coin squeezes (idiosyncratic); overlaps tsmom_trend_following and breakout_20d_overlay (this card is the book-level hedge use)
- fit: Med — free and simple, but needs longs enabled (docs/LONG_BUILD.md)

### alt_index_trend_ewmac — Market (alt-index) trend applied to every coin
- direction: both
- family: trend
- mechanism: crypto moves as one factor: the market's volatility-normalised trend forecasts each coin better than the coin's own noisy trend (Carver's "asset-class trend")
- rules: index = cumulative sum of the daily cross-sectional median of r_i/sigma_i over the top 50 perps; F = EWMAC on the index with fast/slow 16/64, 32/128, 64/256 days, each scaled to average |F| = 10 and capped at +-20; apply the same F to every coin, sized by each coin's own volatility; a continuous replacement for the binary BTC 60-day gate (gate G)
- data needed: daily klines: Binance public
- evidence: Carver's assettrend rule (systems/provided/rules + rob_system/rawdata.py normalised_price_for_asset_class), weight 0.096 in his BITCOIN allocation; a practitioner configuration, no statistics in the repo. Our own BTC 60-day gate (docs/LONG_PLAYBOOK.md) is its binary cousin (learning-repos/pysystemtrade (Carver, GPL-3: formulas reimplemented, never copied), repo study 2026-10-04)
- grade: Single-source
- risks: everything flips together, so it adds no diversification beyond the gate; whipsaw in ranges
- fit: High — a continuous gate usable as a shared forecast in both sleeves

### ewmac_acceleration — Trend acceleration
- direction: both
- family: trend
- mechanism: the change in trend strength leads price at trend starts and ends
- rules: E = EWMAC(price, sigma, Lf, 4Lf) on daily closes; A = E_t - E_(t-Lf), Lf in {8, 16, 32}; scale to average |F| = 10, cap +-20; Carver sizing; 10% buffer. For the short book: a decelerating trend after a pump as an entry or exit timer
- data needed: daily klines: Binance public
- evidence: Carver's accel rule (rules/accel.py, forecast scalars 7.8/5.6/3.9); shipped, but weight 0 for BTC in his own configuration (the cost "speed limit"). No statistics in the repo (learning-repos/pysystemtrade (Carver, GPL-3: formulas reimplemented, never copied), repo study 2026-10-04)
- grade: Single-source
- risks: high turnover; whipsaw in chop; correlated with fast EWMAC
- fit: Med — cheap to test as a pump-fade timing input

### skew_premium_xs — Cross-sectional skewness premium
- direction: neutral
- family: cross-sectional
- mechanism: investors overpay for positive skew (lottery demand), so negative-skew assets earn a premium
- rules: s = -skew of daily % returns over 180 days; demean by the universe median each day; F = EWMA(45) of s / robust volatility of s; long the top quintile, short the bottom; volatility-target each leg; test orthogonal to max_lottery_short
- data needed: daily klines, top ~60 perps: Binance public
- evidence: Carver's skew rules (rules/factors.py, rob_system/rawdata.py), weight 0.16 across four variants in his BITCOIN allocation; a practitioner configuration, no statistics in the repo (learning-repos/pysystemtrade (Carver, GPL-3: formulas reimplemented, never copied), repo study 2026-10-04)
- grade: Single-source
- risks: crowds with max_lottery_short; squeezes on the short leg; 180-day windows give few independent periods
- fit: Med — a persistent distribution trait, unlike MAX's one-month extreme

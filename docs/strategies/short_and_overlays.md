# New short setups, regime overlays and execution aids (library, research 2026-10-01)

Format and status rules: [README.md](README.md). These extend the current playbook ([../PLAYBOOK.md](../PLAYBOOK.md)).
Most are features or vetoes for existing setups rather than new setups.

## New short setups

### supply_overhang_short — Short supply created by market-maker deals, discount buyers and unlocks
- direction: short
- family: event/supply
- mechanism: project-to-MM token loans (~1% of supply for ~12 months, call strikes up to +100%) and 5-15% discount OTC "investments" (DWF: $100-150k daily tranches over 1-3 months) create hedged, informed sellers; cliff unlocks add VC/team supply; buyers are retail
- rules: same as listing_fade / unlock_short, ranked by supply: short 2-4 weeks before large cliff unlocks (> 2% of supply) or 1-14 days after a perp listing once initial volatility settles; stop above the listing/swing high + 1 ATR; time stop at the unlock date or 30 days; low-float (< 30% circulating) tokens with Binance perps. Treat "MM investment" announcements as supply events
- data needed: exchangeInfo onboardDate, klines, funding, OI: Binance public; unlock calendars and MM-deal disclosures manual
- evidence: deal mechanics (https://www.theblock.co/post/267354/how-dwf-labs-makes-deals-and-its-tendency-to-talk-about-price, https://www.coindesk.com/business/2023/04/14/market-maker-dwf-labs-more-than-200m-in-deals-blur-what-investing-means); Gotbit wash-trading conviction 2025 (https://www.justice.gov/usao-ma/pr/cryptocurrency-financial-services-firm-gotbit-and-founder-sentenced-market-manipulation); 2025 listings lost ~70% by Mar-2026 (see airdrop_hodler_supply_short). Keyrock, 16,000 unlocks over 40 tokens (verified 2026-10-02): team unlocks ~−25%, pressure from ~30 days before, 5–10% unlocks drop 2.4× more sharply, recovery ~14 days after (https://keyrock.com/from-locked-to-liquidity-what-16000-token-unlocks-teach-us/)
- grade: Practitioner-data
- risks: MMs also pump low floats (squeezes); monitoring tags flip sentiment; crowded shorts mean negative funding; unlocks get priced early
- fit: High — this is the institutional mechanism behind our listing_fade and unlock setups

### airdrop_hodler_supply_short — HODLer-airdrop, Launchpool and Alpha-graduate supply short
- direction: short
- family: event/supply
- mechanism: airdrop and launchpool recipients (BNB holders, farmers, Alpha users) hold free tokens and sell into the listing and the following weeks; low float and unlocks add overhang
- rules: *(proposed)* tokens listed via Binance HODLer Airdrop / Launchpool / Alpha -> Futures in the last 30d; wait >= 24-48h (listing_fade rules); short the first lower high below the day-1 VWAP; time stop 21d; skip if OI is tiny or funding < -0.1%
- data needed: latest_announcements, klines, OI, funding: Binance public
- evidence: 2025 listings held from day-1 close to 11-Mar-2026: Binance n=92 avg -71.7%; Upbit n=59 -69.5% (median -80.9%, only 2 profitable); Bithumb n=144 -69.1%; H2-2025 cohorts decayed less (Bithumb -59.4% vs -77.3%) (https://www.odaily.news/en/post/5209720)
- grade: Practitioner-data
- risks: overlaps listing_fade; violent squeezes in thin floats; the effect weakened in H2-2025
- fit: High — a source tag for the listing_fade pipeline, using data we already fetch
- logged: since 2026-10-02 every scan logs `listing_source` (hodler_airdrop / launchpool / megadrop / alpha / seed_spot / spot / futures_only) for perps listed <= 90 days, via signals/supply.py; zero score impact until research_report shows an effect

### lowfloat_highfdv_decay_short — Low-float, high-FDV decay
- direction: short
- family: event/supply
- mechanism: tokens launch with ~10-20% circulating at a very high FDV; ~$155B of unlocks are scheduled 2024-2030 against little organic demand, so they bleed for months
- rules: *(proposed)* Binance perps listed < 180d with circulating/total < 25% and FDV > $1B; short the first lower high after a 30d high; small size; hold 2-8 weeks; cover before major positive catalysts; stack with unlock calendars
- data needed: circulating/total supply (CoinGecko/CMC, not Binance), klines
- evidence: Binance Research, "Low Float & High FDV" (May-2024): ~US$155B unlocking 2024-2030 (https://www.binance.com/en/research/analysis/low-float-and-high-fdv-how-did-we-get-here); 2025 listing cohort -70% (above)
- grade: Practitioner-data
- risks: thin float means violent squeezes; overlaps listing_fade and unlock
- fit: High — a float/FDV ranking feature for listing_fade and unlock candidates
- logged: since 2026-10-02 every scan logs `float_ratio`, `fdv_usd`, `mcap_usd`, `fdv_mcap` (CoinGecko weekly cache, signals/supply.py); zero score impact

### upbit_warning_short — Upbit/Bithumb "investment warning" short
- direction: short
- family: event/supply
- mechanism: Upbit's warning designation often precedes a KRW delisting; Korean retail, often the main bid, exits while the Binance perp keeps trading
- rules: *(proposed)* on the warning notice, short the Binance perp on the first 1h lower high; hold through the review (~1-4 weeks); cover on warning removal; exit before the final delisting day if Binance also delists
- data needed: Upbit notice feed (public, external), Binance klines/OI
- evidence: none quantified; the second-venue mirror of our delisting short
- grade: Anecdote
- risks: warning lifted means a sharp squeeze; low-liquidity perps
- fit: Med — extends delisting with a second-venue trigger; needs an Upbit notice poller

### hack_exploit_short — Short a protocol token after an exploit (post-announcement drift)
- direction: short
- family: event/supply
- mechanism: the hack is visible on-chain before disclosure; price keeps falling for hours after it becomes common knowledge as slower holders exit and TVL drains
- rules: on a credible report (protocol account, PeckShield, CertiK) of a loss >= 5% of market cap or TVL, short within the first 1-4h while price is within 15% of pre-news; stop above the pre-news high; hold ~12-15h, exit by 24h (or target 20-30%, time stop 5d); skip on a full-reimbursement announcement
- data needed: news (coin_news), klines: Binance public
- evidence: 49 DeFi hacks Jun-2016 to Jan-2025: 24h CAR ~-27%; -17.2% by hour 3 and -19.1% by hour 6 after the on-chain hack; 36% of the decline precedes the announcement; ~-13 pp drift after disclosure (NY Fed SR 1153, https://doi.org/10.59576/sr.1153)
- grade: Working-paper
- risks: n=49; many hacked tokens have no perp or get halted; reimbursements and bounties cause violent squeezes; funding goes deeply negative
- fit: Med — rare but large, and our news tools already see it
- logged: since 2026-10-02 every scan logs `hack_flag` with the matched headline, source, URL, age and a reimbursement flag (signals/hack.py, 72h window); shadow only

### eth_exchange_inflow_short — ETH exchange inflows, short ETH
- direction: short
- family: event/supply
- mechanism: holders move coins to exchanges to sell; the supply hits within hours
- rules: hourly; when ETH net exchange inflow is top-decile, short the ETH perp for 1-6h
- data needed: on-chain ETH exchange flows: NOT Binance
- evidence: 2017-2023: ETH net inflows negatively predict ETH returns at every 1-6h horizon; BTC inflows mostly don't (https://arxiv.org/abs/2411.06327)
- grade: Working-paper
- risks: data latency and cost; wallet relabelling
- fit: Med — a clean hourly short signal if a flow feed is ever added

### crowded_long_unwind_short — Short crowded leverage ahead of cascades
- direction: short
- family: microstructure
- mechanism: over-levered longs pay funding and sit on liquidation clusters; a shock forces sales that feed on themselves (10 Oct: 87% of $19.3bn liquidated were longs, $7bn in < 1h)
- rules: *(proposed)* funding > 0.05%/8h for >= 3 prints AND OI +20% in 7d AND price stalls (4h lower high); enter on a break of the 4h swing low; stop above the swing high; target the prior volume node; time stop 3 days; bank 50% on the first > 2 ATR move (ADL risk)
- data needed: funding, OI hist, long/short ratios, taker ratio: Binance public (archive via data.binance.vision metrics)
- evidence: 10-Oct structure (https://insights4vc.substack.com/p/inside-the-19b-flash-crash). own 60-day backtest 2026-07-27 to 09-29 (`run --days60 --flow`, docs/BACKTEST.md "Positioning cards"): last 3 prints >= 0.05%/8h, OI +20% / +40% over 7 days, 4h lower high, break of the 4-bar low: 87 / 63 trades, -0.20 / -0.24R, paired -0.04 / -0.28 (t -0.2 / -1.8), stress -0.24 / -0.27. Fails. With the regime studies (high funding is trend-on, a coin's own funding has no edge), crowded longs in this form are not a short signal **Repo study 2026-10-04 (negative, out of sample):** Jansen's Binance-perps case study ranked 19 perps every 8h on premium, funding and volatility. The chosen setup (1 of 2,807 candidates) had a validation Sharpe of 1.67 (PSR p 0.005), but a Deflated Sharpe below 0 under every trial count, then a 2024-25 holdout Sharpe of -0.83 (CAGR -45%, max DD -82%); execution costs ran more than 4x the funding earned (learning-repos/machine-learning-for-trading/case_studies/crypto_perps_funding (Jansen, MIT)).
- grade: Practitioner-data
- risks: crowded longs stay crowded for weeks; squeezes on low float; the winning short gets ADL'd in the very cascade it predicted
- fit: Low — tested and failed (above); keep only as context in plans

### forced_flow_crowding_sizing — Calendar forced flows are crowded: size them down
- direction: both
- family: event/supply
- mechanism: index adds/deletes, delistings, listings and unlocks force trades on known dates; pods and MMs harvest them, so the edge is real but crowded
- rules: keep using the delisting/listing/unlock playbooks; enter after the announcement shock fades, exit before the forced date; cap per-event risk below normal
- data needed: exchangeInfo deliveryDate/onboardDate, announcements, unlock calendars
- evidence: Millennium lost ~$900m on index add/delete bets in Feb-2025 (-1.3%, its worst month in 6+ years) despite best-in-class risk limits (https://www.hedgeweek.com/millennium-posts-rare-monthly-loss-amid-index-rebalancing-trades/) **Repo study 2026-10-04:** sizing choice alone moved crypto Sharpe by 0.908 across six allocators in Jansen's case studies: count sizing variants as trials.
- grade: Practitioner-data
- risks: everyone sees the same calendar; squeezes into delivery
- fit: Med — a sizing rule for setups we already run

## Regime overlays and vetoes

### carry_crash_regime — High aggregate carry = crash-risk regime
- direction: both
- family: carry
- mechanism: small trend-chasers bid for leveraged longs while arbitrage capital is scarce; extreme carry marks crowded, fragile longs, and crashes follow
- rules: daily OI-weighted annualised funding/basis on BTC+ETH (and an alt aggregate); top decile (funding > 0.03%/8h for days, or quarterly basis > 20-25% ann.) -> allow larger short exposure on high-beta alts and cut longs; low or negative carry -> cut new shorts, long bias. Use for sizing and stops, not timing
- data needed: funding, premiumIndex, delivery prices: Binance public
- evidence: BTC/ETH futures carry averages > 10% a year and peaks near 60%; high carry predicts future crashes, more negative skew and costlier crash insurance; a $1 rise in basis predicts ~$5 lower spot to maturity; +10% carry predicts +44% (of OI) more sell-side liquidations next month (Schmeling-Schrimpf-Todorov, BIS WP 1087, https://www.bis.org/publ/work1087.htm). **REFUTED on Binance perps 2020-07 to 2026-08 (own study, docs/REGIME_STUDIES.md, task #12):** with BTC/ETH carry in its top quintile, the alt basket short over 7 days did 4.4% WORSE than other days (Newey-West t -3.1, same sign in 4 of 5 years), 30-day alt crashes (basket <= -25%) were LESS frequent (12.5% vs 24.0%, t -1.9), and top-gainer fades showed no regime effect (|t| < 1.3). High carry is trend-on here, not crash risk; see high_funding_trend_confirm **Repo study 2026-10-04:** the same panel's regime split: premium effect ~10x larger in high volatility, t -1.56, not significant (learning-repos/machine-learning-for-trading/case_studies/crypto_perps_funding (Jansen, MIT)).
- grade: Working-paper
- risks: carry stays high for months in bull runs; monthly horizon; Ethena-scale capital may cap future extremes
- fit: Low — retired as stated (the opposite holds on our venue); the evidence moved to high_funding_trend_confirm

### high_funding_squeeze_veto — Rising coin funding: size down, don't exclude
- direction: short
- family: carry
- mechanism: rising carry predicts more short-side liquidations and higher volatility before any crash
- rules: if a coin's funding or basis is top-decile AND rising over 24-72h, halve size or require a confirmed lower high, and widen stops
- data needed: funding, OI, klines: Binance public
- evidence: BIS WP 1087 (above); retail crypto traders chase momentum (Kogan et al. JFE 2024, https://doi.org/10.1016/j.jfineco.2024.103897). Own study (task #13, 21k events, 2020-2026): a coin's funding >= 0.05% or 0.10% per 8h has NO short edge against same-class peers over the same window, in any class (majors, pumped alts, other alts), raw or after a breakdown, at 1/3/7 days: 36 cells, none 'avoid', best |t| 2.24 (chance level for 36 trials) **Repo study 2026-10-04:** in Jansen's Binance-perps panel the premium effect was ~10x larger in high-volatility regimes (-0.0024 vs +0.0002 per z-unit) but not significant (t -1.56) (learning-repos/machine-learning-for-trading/case_studies/crypto_perps_funding (Jansen, MIT), 04_dml_crypto_regime).
- grade: Practitioner-data
- risks: may veto the best crash shorts; the market-level effect is the aggregate-carry regime, not the coin's own funding
- fit: Low — no coin-level evidence; keep coin funding as a flag (funding budget, tailwind), use the aggregate-carry rule in high_funding_trend_confirm for sizing

### liquidation_wick_harvest — Deep resting reduce-only take-profits to catch cascade wicks
- direction: both
- family: microstructure
- mechanism: in cascades liquidation engines market-sell into books MMs have emptied; resting bids far below market fill at "completely ridiculous" prices (Wintermute's Gaevoy); Hyperliquid's HLP is paid exactly this as backstop
- rules: for every open short, ladder GTC reduce-only take-profit buys at -5% / -10% / -20% below mark; optional long version: tiny resting bids 15-30% below on high-OI alts in high funding/OI regimes, exit at a 50% wick retrace or a 4h time stop
- data needed: OI hist, funding, depth, !forceOrder@arr: Binance public
- evidence: HLP +$40-41.5m (~+10%) on the 10-11 Oct 2025 weekend and +$15m on 31-Jan-2026; the two events ~41% of its $136.9m lifetime profit (https://www.coingecko.com/learn/hyperliquid-hlp-vault-analysis); 10-Oct prints ATOM ~$0.001, TON -80%, WLD -70% (https://coinshares.com/us/insights/knowledge/billions-in-liquidations-what-happened/)
- grade: Practitioner-data
- result: own test 2026-10-02 (`backtest/rungs.py`, docs/PROPOSALS.md P1a): carving rungs out of automatic targets LOSES in all three samples (hourly −0.005 to −0.010R, t to −2.4; random −0.018 to −0.026R; daily 2020-26 to −0.011R). Rungs added to a runner WIN slightly: 6/12/20% at 50% size +0.005R (t 3.9) hourly, +0.004R daily. Keep rungs for the part of a position with no automatic target
- risks: rarely fills; the venue may bust trades; outages block cancels; must not race the guardian's own exits
- fit: High — reduce-only ladders on shorts are nearly free and monetise exactly the 10-Oct wicks; the long version needs a long book

### oi_price_funding_regime — OI / price / funding divergence classifier
- direction: both
- family: microstructure
- mechanism: who is adding risk. OI up + price down + funding positive = longs averaging down (long trap, short continuation); OI up + price down + funding negative = late shorts piling in (squeeze fuel, long on reclaim)
- rules: *(proposed)* 8h windows; long trap needs >= 2 positive 8h funding prints before shorting; squeeze fuel -> long only after a reclaim of local resistance; flat funding = no trade
- data needed: openInterestHist (30d), funding, klines: Binance public
- evidence: framework only (https://coinunited.io/en/research/crypto/crypto-open-interest-divergence-signals-guide-2026); BTC OI +12% in a month with negative funding preceded a squeeze attempt, Feb-2026 (CoinDesk). own 60-day backtest 2026-07-27 to 09-29 (`run --days60 --flow`, docs/BACKTEST.md "Positioning cards"):
  - long trap short (OI +5% over 24h, price -3%, last 2 prints > 0): 244 trades, -0.01R absolute, but +0.10R over the matched same-hour placebo (t 1.7, both halves +0.10 / +0.11). That is a relative edge, like pump_fade's; with OI +10%: +0.08 (t 1.3).
  - squeeze fuel long (OI +10%, price -3%, last 2 prints < 0, then a 12h-high reclaim): 49 trades, +0.34R, stress +0.31, DSR 0.98, PF 2.56, +0.85% funding received per trade. But it is only +0.13R over its placebo (t 0.3): "promising", small n. The OI +5% variant: 86 trades, +0.30R, +0.09 vs placebo.
- grade: Practitioner-data
- risks: unvalidated (OI history: REST 30d, full archive via data.binance.vision metrics)
- fit: Med — the long-trap short has a relative edge only (it needs a hedge or a falling regime), and the squeeze-fuel long is the best long so far but n=49. Shadow-log both and retest on new data

### ls_ratio_taker_contrarian — Retail long/short ratio and taker-flow contrarian filter
- direction: both
- family: microstructure
- mechanism: the global account ratio counts retail heads; the top-trader position ratio tracks large accounts; retail heavily short while top traders are long and taker buys dominate tends to squeeze up (mirror for shorts)
- rules: *(proposed)* 1h; long when globalLongShortAccountRatio < 0.8 (or 7d z < -2), topLongShortPositionRatio rising and takerlongshortRatio > 1.1 for 3 bars above the 1h VWAP; mirror as a short confirmation; exit when the ratio normalises
- data needed: /futures/data/globalLongShortAccountRatio, topLongShortPositionRatio, takerlongshortRatio: Binance public (REST keeps 30 days; full history in data.binance.vision daily `metrics`, parsed by histdata_vision.py)
- evidence: no published backtest; used descriptively by CoinGlass and CoinDesk. own 60-day backtest 2026-07-27 to 09-29 (`run --days60 --flow`, docs/BACKTEST.md "Positioning cards"):
  - short side (retail account ratio z >= 2, top traders cutting, takers selling): 53 trades, -0.19R, -0.06 vs placebo. Fails.
  - long side (z <= -2 / -3): 1,246 / 273 trades, +0.00 / +0.03R, paired -0.02 / +0.01. No edge.
  - Consistent with round 0's feature split: who is long (whales vs retail) did not matter at 1h.
- grade: Practitioner-data
- risks: noisy and venue-specific; account ratio is not notional
- fit: Low — tested, no edge on either side in the 60-day window

### cross_exchange_premium_sentiment — Korea premium as a froth gauge
- direction: short
- family: stat-arb
- mechanism: capital controls segment markets; a retail-driven local premium marks speculative excess
- rules: premium = Upbit KRW price / (Binance USDT price x USDKRW); premium spikes on Upbit-listed alts = froth filter for shorts
- data needed: Upbit public API + FX (free, not Binance)
- evidence: large, recurrent cross-country spreads lasting days to weeks, driven by capital controls (Makarov-Schoar JFE 2020, https://doi.org/10.1016/j.jfineco.2019.07.001); no direct return-predictability test
- grade: Peer-reviewed
- risks: premiums stay high for weeks
- fit: Med — free data, a good overlay for alt shorts

## Execution aids (not strategies)

### reversal_15m_flow_execution — 15-min reversal after aggressive taker flow
- direction: both
- family: microstructure
- mechanism: compensated liquidity provision: bars driven by aggressive takers partly reverse next bar
- rules: bet against the previous 15m candle, strongest after top-quintile |taker imbalance| bars; use only to place short limit entries just after an aggressive taker-buy bar instead of chasing
- data needed: 15m klines with takerBuyBaseVolume: Binance public
- evidence: 90% of 183 Binance pairs show significant out-of-sample reversal (vs 2.7% of US stocks) every coin-year since 2021, on USDⓈ-M perps too; gross edge ~1.3 bp/trade vs 5 bp round trip (Kitron-Wengrowicz, https://arxiv.org/abs/2608.21888)
- grade: Working-paper
- risks: edge below taker cost
- fit: Med — free timing for maker entries

### funding_settlement_drift — Drift around funding timestamps
- direction: both
- family: microstructure
- mechanism: carry harvesters open just before 00/08/16 UTC and close just after; with large funding this creates a small predictable drift, and the post-settlement pressure reverses
- rules: *(proposed)* for |predicted funding| > 0.05%: avoid entering on the payer side in the last 30-60 min before settlement; lab test: short T-60m to T+0 when funding > +0.1%, mirror when < -0.1%; fade the 15-60 min move into settlement when funding is extreme, exit within 1-2h
- data needed: premiumIndex (predicted rate, nextFundingTime), 1m/1h klines: Binance public
- evidence: **Own test** (§4, 60d, clustered by print): into prints with |funding| >= 0.1% the payer side GAINS +0.19% (t −2.69 by hour, −2.71 by day), and in the 2h AFTER it gains +0.29% (t −3.14 / −2.77; 1,046 prints; #31 pooled-mean t, 2026-10-02): high funding keeps trending through the print, the opposite of the card. Rule: don't short a high-funding coin into or right after the print. Literature: settlement-tied intraday spread patterns, spreads peak ~2h after settlement (https://www.mdpi.com/2227-7072/14/5/103, snippet only); Polymarket 5-min BTC contracts show settlement flow spikes and reversals (https://arxiv.org/abs/2606.31675, different venue)
- grade: Single-source
- risks: tiny vs fees; hourly-funding symbols blur the timestamps
- fit: Med — a zero-cost lab test on stored data; at least an execution-timing rule

### intraday_hour_seasonality — Hour-of-day seasonality
- direction: both
- family: other
- mechanism: recurring flows by time zone and session (funding times, US/Asia opens)
- rules: signal by hour of day on majors; parameters not published
- data needed: 1h klines: Binance public
- evidence: **Own test** (§4, 60d): UTC 04 / 11 / 16 / 23 positive (16 UTC +21bp, day-clustered t 3.9); the hourly profile correlates 0.48 between halves. Robot Wealth research strategy, no public stats (https://robotwealth.com/index-of-strategies/)
- grade: Practitioner-data
- risks: small vs taker fees; regime-dependent
- fit: Low — an entry-timing tweak only

### orderbook_imbalance_l2 — L2 depth state and order-flow imbalance
- direction: both
- family: microstructure
- mechanism: thin, one-sided books predict short-horizon moves; order flow matters only when liquidity is stressed
- rules: classify the top-20 L2 state (calm/mixed/stressed) before events; minutes horizon
- data needed: depth snapshots/diff-depth websocket: Binance public, live only
- evidence: Binance BTC/ETH 2023-2026, rolling monthly OOS: pre-event L2 state is the first-order predictor; flow helps ETH under stress; BTC "no regime that clears at both horizons"; no PnL (https://arxiv.org/abs/2607.09230)
- grade: Working-paper
- risks: latency; edge lives at seconds to minutes
- fit: Low — at most a "thin book: smaller size, wider stop" check (our depth logging, learning #31)

### investor_base_vol_sizing — Investor-base growth raises idiosyncratic volatility
- direction: both
- family: volatility
- mechanism: new, less-informed holders trade noisily
- rules: if a coin's community/follower growth spikes, cut size or widen stops in proportion to expected vol
- data needed: Reddit/X follower counts: not Binance
- evidence: 355 coins Jan-2014 to Jul-2022: subreddit follower growth significantly raises idiosyncratic vol (https://arxiv.org/abs/2211.13274) **Repo study 2026-10-04:** in Jansen's nine case studies, crypto had the largest spread between allocators: 0.908 Sharpe between the best and worst of six sizing methods. Sizing choices are trials too and must count toward N.
- grade: Working-paper
- risks: vol, not direction; social data access
- fit: Low — marginal over realised-vol sizing

### liquidation_magnet — Liquidation-cluster magnet and sweep-reverse
- direction: both
- family: microstructure
- mechanism: dense estimated liquidation levels attract price as hunters push into them for liquidity; after the sweep price often reverses
- rules: *(proposed)* estimate clusters from OI build-up zones x typical 10-25x leverage; trade the reversal after a sweep with a 1h close back inside; stop beyond the wick
- data needed: CoinGlass heatmaps (paid estimates); Binance only live liquidation prints + OI hist
- evidence: no rigorous public study
- grade: Anecdote
- risks: estimated levels are guesses; cascades run far past them (10 Oct)
- fit: Low — keep as a qualitative "where are the stops" note in plans (our sweep_high detector covers the measurable part)

### cme_gap_fill — CME weekend gap fill (BTC)
- direction: both
- family: other
- mechanism: CME closes on weekends; the perp gaps vs Friday's close; folklore says Monday re-hedging fills it
- rules: *(proposed)* Sunday 22:00 to Monday UTC: if |perp - CME Friday close| > 2%, fade toward the gap, 50% target, time stop Tuesday close
- data needed: CME close (external), perp klines
- evidence: none verified; unfilled gaps are forgotten (selection bias)
- grade: Anecdote
- risks: gaps stay open for months
- fit: Low — external data, weak evidence

## Anti-cards (documented so we don't rediscover them)

### llm_discretionary_perps — An LLM making discretionary perp calls
- direction: both
- family: other
- mechanism: none identified: no price-insensitive counterparty, and LLMs are weak on numeric time series
- rules: Nof1 Alpha Arena: $10k per model, Hyperliquid perps, identical prompts and data, autonomous decisions
- data needed: n/a
- evidence: Season 1 (Oct-Nov 2025): 2 of 6 models positive (+22%, +5%), 4 lost 31-63%; Season 1.5: 6 of 32 accounts positive, -35% overall, and the S1 winner lost in all 4 runs; the S1 winner made ~43 trades (< 3/day) (https://www.iweaver.ai/blog/alpha-arena-ai-trading-season-1-results/, https://github.com/josejuarez96/tradepartner/pull/233) **Repo study 2026-10-04:** Microsoft's RD-Agent(Q) (an LLM factor/model research loop, NeurIPS 2025) claims ~2x annualised return over factor libraries for under $10; those are the authors' numbers on CSI300 over one fixed test window, accepted by LLM judgement without significance tests (learning-repos/RD-Agent).
- grade: Practitioner-data
- risks: overtrading, fees, rank instability
- fit: Low — evidence against AI judgement as the alpha source. Keep the LLM on research, ops and executing explicit rule-based signals (this argues for rules, not for approval gates)

### sadf_lppls_bubble_short — Short after the explosive regime ends (SADF timing)
- direction: short
- family: other
- mechanism: a pump is an explosive price path; shorting while it is still explosive is where squeezes happen. A supremum ADF (SADF) test flags the explosive root while it is live; shorting the first breakdown after SADF falls back under its critical value targets the decay and skips the squeeze tail our pump shorts suffer
- rules: 1h log closes; SADF on a rolling 30-day window (min window 48 bars, 1 lag), critical values simulated per coin under a random-walk null; bubble = SADF above the 95% critical value for >= 6h; short at the first 1h close where SADF is back below it AND the close is under the prior 6h low; stop at the bubble high + 0.5 ATR(1h); exit at a 50% retrace of the bubble leg or 72h. Variant: also require the LPPLS critical time within +-12h. Placebo: same-hour pump_fade entries without the SADF condition
- data needed: 1h klines: Binance public (data.binance.vision, our stream store)
- evidence: method only, no crypto result. SADF from López de Prado, AFML ch. 17 (mlfinlab's API lists it: learning-repos/mlfinlab), Phillips-Shi-Yu explosive-root tests; LPPLS from Sornette's bubble work (the lppls package in awesome-quant). Unverified on crypto (repo study 2026-10-04, docs/QUANT_METHODS.md 'What the repos taught')
- grade: Single-source
- risks: critical values are sensitive to window and fat tails; re-ignition (a re-pump is the tail itself); LPPLS is over-parameterised; compute per coin-hour
- fit: High — free data, aimed straight at the measured squeeze tail; runs as a pump_fade timing veto in the existing card harness

### vpin_toxicity_squeeze_veto — Order-flow toxicity veto for pump shorts
- direction: both
- family: microstructure
- mechanism: persistently one-sided aggressive buying (informed or forced short-covering) keeps pushing price and shows as high VPIN and a rising Kyle's lambda (impact per signed volume); fading it is where squeezes come from. Toxicity falling off a peak marks exhaustion
- rules: volume buckets = 1/50 of the coin's daily volume from 1m taker-buy volume (Binance gives the true aggressor side); VPIN = mean |buy - sell| / bucket volume over 50 buckets; lambda = slope of 5m returns on signed taker volume over 24h. On pump_fade candidates: veto while VPIN is above the coin's own 30-day 0.9 quantile and lambda is rising; allow entry only after VPIN falls below the 0.7 quantile from a peak above 0.9. Compare vetoed vs allowed R against a random veto at the same rate, a plain volume-surge veto and an |return| veto
- data needed: 1m klines with taker-buy volume (our stream store); aggTrades for exact buckets: Binance public
- evidence: method only, no crypto result: VPIN from Easley, López de Prado and O'Hara (2012); its predictive power is disputed (Andersen and Bondarenko 2014). Implementations: mlfinlab's API (get_vpin; AFML ch. 19), orderflow-metrics (awesome-quant), a NautilusTrader example (hurst_vpin_directional). ML4T ch. 3/8 has Kyle's lambda and Roll / Corwin-Schultz spreads (repo study 2026-10-04, docs/QUANT_METHODS.md 'What the repos taught')
- grade: Single-source
- risks: VPIN may only proxy volume and volatility, so it must beat those controls; the veto may shrink the sample without moving the mean
- fit: High — the data is already streamed and it attacks squeezes directly

### dollar_bar_hurst_vpin_trend — Hurst trending regime plus VPIN flow on dollar bars
- direction: both
- family: microstructure
- mechanism: informed order flow is persistent: when the rescaled-range Hurst exponent of dollar-bar returns says trending and VPIN says aggressor flow is one-sided, price continues in the flow's direction
- rules: dollar bars of fixed notional; Hurst = R/S over 128 bars at lags 4/8/16/32; VPIN over 50 buckets, threshold 0.30; enter IOC in the direction of the signed imbalance when Hurst > 0.55 and VPIN >= 0.30 with a consistent sign; exit when Hurst < 0.50 or after 3,600 s
- data needed: aggTrades with isBuyerMaker (Binance public) to build dollar bars
- evidence: none: a template in NautilusTrader (crates/trading/src/examples/strategies/hurst_vpin_directional, LGPL-3), no backtest results. Our own hourly triggers failed against placebos, and this is faster still (repo study 2026-10-04, docs/QUANT_METHODS.md 'What the repos taught')
- grade: Anecdote
- risks: intraday trigger plus taker fees on every entry; VPIN correlates mechanically with volume and volatility; R/S Hurst on 128 points is noisy
- fit: Low as a strategy; its two measures are better used as squeeze-risk features (vpin_toxicity_squeeze_veto)

### ewmac_wings_fade — Fade 3-sigma fast trends
- direction: both
- family: stat-arb
- mechanism: extreme short-horizon moves overshoot and then mean-revert
- rules: E = EWMAC(4, 16) on 4h bars; s = expanding std of E (>= 500 observations); F = -E where |E| > 3s, else 0; time stop 3 days
- data needed: klines: Binance public
- evidence: Carver's mr_wings rule (rules/mr_wings.py); shipped but not in his live configuration, no statistics (learning-repos/pysystemtrade (Carver, GPL-3: formulas reimplemented, never copied), repo study 2026-10-04)
- grade: Anecdote
- risks: a systematic pump fade, so squeezes are its tail (which our shorts already suffer); fat tails make 3 sigma common
- fit: Low — only as a sizing input to pump_fade, not a standalone sleeve

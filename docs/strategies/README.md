# Strategy library

Every strategy the desk has read about, whether it trades long, short or market-neutral, and whether or not we run it. Gathered 2026-10-01 from six research passes: the 1Token quant indices, crypto funds and market makers, TradFi quant firms, crypto-quant structure, academic papers and practitioner backtests. Industry benchmarks and lessons: [../INDUSTRY.md](../INDUSTRY.md).

The live, short-only playbook is still [../PLAYBOOK.md](../PLAYBOOK.md) plus the skills. This library is the pipeline that feeds it.

## How it works
- **One card = one strategy**, in a strict format: `### id — Name`, then `- direction / family / mechanism / rules / data needed / evidence / grade / risks / fit` bullets.
- **Registry:** `shortbot registry seed` registers each card as `lib.<id>` (level `strategy`, status **draft**, metric `trade_r`, no detector yet), and `shortbot registry search TEXT` finds it. Editing a card's rules creates a new version. Code: `src/shortbot/library_seed.py`.
- **Path to live:** draft → shadow (a detector in `signals/` or the backtest, logging episodes) → live (the registry's promotion gate: ≥ 30 coins, CI > 0, t ≥ 3). See [../REGISTRY.md](../REGISTRY.md).
- **Long and neutral cards** cannot trade until longs are enabled. They can still be backtested and shadow-logged now, from the same data.
- *(proposed)* in a card's rules means the mechanism is sound but nobody has published stats. Lab-test it before trusting it.
- **Grades:**
  - Peer-reviewed;
  - Working-paper;
  - Practitioner-data (real P&L or a backtest with numbers);
  - Single-source;
  - Anecdote.
- **Fit** is for this desk: Binance USDⓈ-M, a small account, hourly bars, public data.

## What the research says, in six lines
1. **Factor sorts are a long tool.** Academic cross-sectional premia live in the long leg (CTREND low quintile t=0.16, post-2020 momentum loser quintile t=0.08). Absolute short alpha needs events, flows and crowding, which is what the desk already trades.
2. **Carry didn't die, it moved.** Mainstream funding fell to ~2.8% ann. (mid-2026), and pro funding-arb books earn 6-8%. The yield now sits in perp-vs-perp dispersion (Liquibit ~10% gross) and TradFi perps (~12.6% funding). For us carry is a **signal**: a crash gate (BIS WP 1087) and a ranking of who pays.
3. **The cleanest forced flow is forced selling, and pros buy it.** Selini's founder (13 years, self-reported) calls shorting alts his "big flaw for many years". The first long sleeve should buy capitulations and liquidation flushes.
4. **Tail risk is venue risk.** On 10 Oct 2025:
   - auto-deleveraging (ADL) closed profitable shorts;
   - Binance marked USDe at $0.65 from its own order book;
   - market makers were locked out for about 2 hours;
   - ATOM printed $0.001.

   Rules that follow: margin in USDT/USDC only, ≥ 2x margin buffer, deep reduce-only TP ladders, partial profits on big winners.
5. **LLM judgement is not the edge.** In Alpha Arena 26 of 32 LLM accounts lost money. Edges should be explicit, rule-based and backtestable, with the agent running research and execution. This argues for rules, not approval gates.
6. **Lumpy is normal.** Pro directional teams won ~40% of trades and lost in 4 of 5 months in 2026 (1Token). The median quant crypto fund made +3.2% in 2025.

## Priority queue (evidence × fit)
| # | card | side | why first |
|---|---|---|---|
| 1 | carry_crash_regime | gate | BIS evidence; free daily data; gates both books |
| 2 | liquidation_wick_harvest | short exits | nearly free; monetises 10-Oct-type wicks |
| 3 | high_funding_squeeze_veto / high_funding_trend_confirm | short sizing | stop fading majors on funding alone |
| 4 | airdrop_hodler_supply_short, lowfloat_highfdv_decay_short, supply_overhang_short | short | ~-70% listing cohort; tags for listing_fade |
| 5 | tsmom_trend_following | gate / long core | peer-reviewed; "only short downtrends" veto |
| 6 | long_liq_flush_reclaim_long, oversold_bounce_majors, capitulation_forced_selling_long | long | first long sleeve: mirrors of bounce_fade, data in hand |
| 7 | ctrend_technical_aggregate, carry_momo_breakout_blend | long / both | strongest long ranking signals |
| 8 | hack_exploit_short | short | NY Fed: -27% 24h CAR |
| 9 | premium_index_reversion, funding_interval_switch | both | same code path as premium_hi / squeeze_exhaustion |
| 10 | ls_ratio_taker_contrarian, oi_price_funding_regime | both | **log now**: the API keeps only 30 days |

## New evidence for existing setups
- **listing_fade / exchange_listing_pump:** 2025 listings held from day-1 close to 11-Mar-2026 lost about 70% (Binance n=92 -71.7%, Upbit n=59 -69.5%, Bithumb n=144 -69.1%). H2-2025 cohorts decayed less (https://www.odaily.news/en/post/5209720). Listing-day abnormal return was +5.7% across 327 listings, +17.5% on Binance (BRL 2019). The cause is that MM inventory hedges, discount buyers and unlocks all sit on the sell side (supply_overhang_short).
- **binance_spot_listing_frontrun:** the "Binance effect" is +41% on announcement day and +73% within 30 days (press summary, anecdote).
- **funding_hi / premium_hi:** BIS finds high carry predicts crashes. But on BTC 2019-2026, entries made at the highest funding had the best 30-day forward returns (z > 2.5: +9.87%, 64.5% win). Keep funding_hi to post-pump alts with a price trigger.
- **squeeze_exhaustion / veto #3:** a capped symbol moves to hourly funding and reverts only after 16 hourly prints under 0.025% (Binance FAQ). That gives a free squeeze-stage feature.
- **pump_fade:** the MAX effect (1.54%/wk, t=2.68) and daily reversal in illiquid coins (>3,600 coins) support it. BTC/ETH-class coins show momentum instead, consistent with "don't fade majors".
- **unlock:** no peer-reviewed effect size was retrieved this round, so measure it in the journal. Keyrock's 16k-unlock study is in PLAYBOOK.md.

## Index (generated: `python -m shortbot.library_seed`)
| id | direction | family | grade | fit | file |
|---|---|---|---|---|---|
| carry_crash_regime | both | carry | Working-paper | High | short_and_overlays.md |
| carry_momo_breakout_blend | both | cross-sectional | Practitioner-data | High | trend_factors.md |
| cs_momentum_large_caps | both | cross-sectional | Peer-reviewed | Med | trend_factors.md |
| daily_reversal_illiquid_momentum_liquid | both | cross-sectional | Peer-reviewed | High | trend_factors.md |
| forced_flow_crowding_sizing | both | event/supply | Practitioner-data | Med | short_and_overlays.md |
| venue_dislocation_fade | both | event/supply | Practitioner-data | Low | carry_neutral.md |
| dislocation_capture | both | microstructure | Practitioner-data | Med | long_side.md |
| funding_settlement_drift | both | microstructure | Single-source | Med | short_and_overlays.md |
| liquidation_magnet | both | microstructure | Anecdote | Low | short_and_overlays.md |
| liquidation_wick_harvest | both | microstructure | Practitioner-data | High | short_and_overlays.md |
| ls_ratio_taker_contrarian | both | microstructure | Anecdote | Med | short_and_overlays.md |
| oi_price_funding_regime | both | microstructure | Anecdote | Med | short_and_overlays.md |
| orderbook_imbalance_l2 | both | microstructure | Working-paper | Low | short_and_overlays.md |
| reversal_15m_flow_execution | both | microstructure | Working-paper | Med | short_and_overlays.md |
| cme_gap_fill | both | other | Anecdote | Low | short_and_overlays.md |
| intraday_hour_seasonality | both | other | Practitioner-data | Low | short_and_overlays.md |
| llm_discretionary_perps | both | other | Practitioner-data | Low | short_and_overlays.md |
| premium_index_reversion | both | stat-arb | Working-paper | Med | long_side.md |
| alt_momentum_burst | both | trend | Single-source | Med | trend_factors.md |
| intraday_tsmom_btc | both | trend | Peer-reviewed | Low | trend_factors.md |
| tsmom_trend_following | both | trend | Peer-reviewed | High | trend_factors.md |
| investor_base_vol_sizing | both | volatility | Working-paper | Low | short_and_overlays.md |
| funding_negative_zscore_long | long | carry | Single-source | Med | long_side.md |
| negative_funding_streak_long | long | carry | Anecdote | Med | long_side.md |
| adoption_growth_long | long | cross-sectional | Peer-reviewed | Low | long_side.md |
| dominance_rotation_long | long | cross-sectional | Anecdote | Low | long_side.md |
| size_small_minus_big | long | cross-sectional | Peer-reviewed | Low | trend_factors.md |
| buyback_burn_long | long | event/supply | Anecdote | Low | long_side.md |
| capitulation_forced_selling_long | long | event/supply | Anecdote | Med | long_side.md |
| korean_listing_announce_scalp | long | event/supply | Anecdote | Low | long_side.md |
| usdt_exchange_inflow_long | long | event/supply | Working-paper | Med | long_side.md |
| long_liq_flush_reclaim_long | long | microstructure | Anecdote | High | long_side.md |
| attention_shock_long | long | other | Peer-reviewed | Low | long_side.md |
| cycle_low_accumulation | long | other | Single-source | Low | long_side.md |
| etf_flow_follow | long | other | Anecdote | Low | long_side.md |
| monday_btc_effect | long | other | Peer-reviewed | Low | long_side.md |
| breakout_20d_overlay | long | trend | Practitioner-data | Med | trend_factors.md |
| ctrend_technical_aggregate | long | trend | Peer-reviewed | High | trend_factors.md |
| high_funding_trend_confirm | long | trend | Single-source | Med | long_side.md |
| oversold_bounce_majors | long | volatility | Anecdote | High | long_side.md |
| coinm_usdm_funding_spread | neutral | carry | Single-source | Low | carry_neutral.md |
| funding_cash_carry | neutral | carry | Practitioner-data | Med | carry_neutral.md |
| funding_interval_switch | neutral | carry | Single-source | Med | carry_neutral.md |
| quarterly_basis_calendar | neutral | carry | Working-paper | Low | carry_neutral.md |
| tradfi_perp_carry | neutral | carry | Practitioner-data | Med | carry_neutral.md |
| xs_funding_carry_basket | neutral | carry | Practitioner-data | Med | carry_neutral.md |
| beta_hedged_rs_pair | neutral | cross-sectional | Working-paper | Med | trend_factors.md |
| dollar_neutral_factor_book | neutral | cross-sectional | Practitioner-data | Med | trend_factors.md |
| listing_basis_arb | neutral | event/supply | Single-source | Med | carry_neutral.md |
| market_making_rebates | neutral | market-making | Practitioner-data | Low | carry_neutral.md |
| range_grid_neutral | neutral | market-making | Anecdote | Low | carry_neutral.md |
| expiry_index_pressure | neutral | other | Single-source | Low | carry_neutral.md |
| forecast_tilted_delta_neutral | neutral | other | Single-source | Low | carry_neutral.md |
| adr_local_perp_triangle | neutral | stat-arb | Anecdote | Low | carry_neutral.md |
| cross_venue_spread_arb | neutral | stat-arb | Single-source | Low | carry_neutral.md |
| pairs_copula_cointegration | neutral | stat-arb | Working-paper | Med | carry_neutral.md |
| perp_spot_basis_convergence | neutral | stat-arb | Working-paper | Med | carry_neutral.md |
| short_vol_options | neutral | volatility | Single-source | Low | carry_neutral.md |
| high_funding_squeeze_veto | short | carry | Working-paper | High | short_and_overlays.md |
| max_lottery_short | short | cross-sectional | Peer-reviewed | High | trend_factors.md |
| airdrop_hodler_supply_short | short | event/supply | Practitioner-data | High | short_and_overlays.md |
| eth_exchange_inflow_short | short | event/supply | Working-paper | Med | short_and_overlays.md |
| hack_exploit_short | short | event/supply | Working-paper | Med | short_and_overlays.md |
| lowfloat_highfdv_decay_short | short | event/supply | Practitioner-data | High | short_and_overlays.md |
| supply_overhang_short | short | event/supply | Practitioner-data | High | short_and_overlays.md |
| upbit_warning_short | short | event/supply | Anecdote | Med | short_and_overlays.md |
| crowded_long_unwind_short | short | microstructure | Anecdote | High | short_and_overlays.md |
| cross_exchange_premium_sentiment | short | stat-arb | Peer-reviewed | Med | short_and_overlays.md |

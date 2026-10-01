# Pattern / manipulation event study

`shortbot study patterns [--fetch]` looks at every tradable perp (460) over 10 days of hourly data: klines with trade count and taker flow, open interest, the premium index, funding, and the spot market where one exists. The code is in `patterns_data.py` (fetch and cache), `patterns_detect.py` (detectors) and `patterns.py` (outcomes and report). It is read-only.

## Detectors (pre-registered; thresholds from the literature, fixed before looking at results)
| kind | definition | source |
|---|---|---|
| pump_kk | Volume above 4× its 12h mean AND the high more than 5% above the 12h mean close | Kamps & Kleinberg 2018, "balanced" set |
| pump_fail | The first red 1h close within 6h of a pump_kk (the "wait for failure" entry) | |
| fake_volume | Volume z-score above 3 over 72h, while \|return\| and range stay below their medians and \|ΔOI\| is under 2% of volume | kline wash-trading proxy (speculative) |
| sweep_high | A wick at least 1.5 ATR past the prior 24h high, then the first close back below it (within 2 bars) | Osler, stop clustering |
| squeeze_up | A 1h return above 3× ATR% while OI falls at least 3% in the same hour | the liquidation-cascade mechanism |
| funding_hi / funding_neg | A funding print of at least +0.05% / at most −0.05% | BIS carry |
| premium_hi | Perp premium at least 0.5% over the index | He et al. |

**Outcome:** a short from the event hour's close, measured at 1h, 4h, 24h and 72h, plus the 24h max adverse and favourable move. Squeeze = adverse move above 15%.

**Rules against fooling ourselves:**
- One event per coin per 24–72h.
- The control is random hours of the same coins, at least 24h from any event.
- The excess return is the short return minus BTC's short return.
- Count distinct coins, not events.
- With about 7 detectors × 5 splits × 6 buckets, roughly 200 cells get tested, so a cell counts only at |t| > 3 **and** if it repeats in a later week.

## First run: 2026-09-19 → 09-29 (one regime; hypotheses only)
- **Nothing beats random hours with |t| > 3** on BTC-excess return. Events are far riskier than random hours: squeeze rate is about 10% vs 1.3%, and p90 adverse move is 15–19% vs 7.5%.
- **Session repeats in two detectors (and matches learning #6):**
  - Pumps and sweeps in Asia, late and weekend hours faded: 24h short win rate 70–100%, mean +2.5–10%.
  - London and NY pumps did not: 44–48%, mean −0.3 to −1.5%.
- **Perp-only coins (no spot market):** pumps faded 69% of the time (mean +3.0%) vs 53.5% (−0.2%) for spot-listed coins. A perp-only pump is pure leverage.
- **Leverage:** pumps with OI up more than 10% in 3h faded more (61%, +1.9%) than OI-flat pumps (57%, +0.1%), but squeezed twice as often (15% vs 8%).
- **Liquidity:**
  - OI under $5M: pumps 63% win (10% squeeze), sweeps 65% (7% squeeze). No worse than the $30M+ buckets.
  - The $10–20M bucket was worst for both (n about 20–30).
  - This week's data gives no support for the $30M OI floor. Depth and spread are the real capacity limits (see learning #13).
- **fake_volume:** only 1 event on 1h klines. Detecting wash trading needs aggTrades (data.binance.vision), not klines.
- **pump_fail:** waiting for the first red close did *worse* than random hours (t −2.05), so it gets no edge credit.

**Next:** rerun weekly with `--fetch` and keep only the cells that repeat. Add aggTrades-based wash tests and a same-timestamp cross-coin control.

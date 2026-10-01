# Reading the tape: order flow and manipulation signals (research 2026-09-28)

Evidence labels: **PR** peer-reviewed, **WP** working paper, **PRAC** practitioner only.

**The headline:**
- No peer-reviewed crypto study tests "sweep, then reversal" directly.
- The strongest evidence says taker order flow **continues** rather than reverses. Crypto order flow has a permanent price impact (J. Financial Markets 2026).
- Crowded leverage leads to crashes over **weeks**, not minutes (Schmeling-Schrimpf-Todorov, Management Science).

**So in `signals/tape.py`:** tape signals start mostly as **vetoes** with small score weights, and `research_report` decides what earns more weight.

| # | Signal | Definition (defaults) | Use | Evidence |
|---|---|---|---|---|
| 1 | Failed sweep | `high > L` and `close < L` within 1–3 bars. L = prior N-bar high (N 20–48) or a round number R (also `high > R·1.001–1.005`). Confirmed by a taker-buy spike on the pierce, OI rising then falling, and short liquidations | bearish trigger | Osler 2003/05 (FX, PR); BTC round-number clustering but no return pattern (Urquhart 2017, PR) |
| 2 | Accepted breakout | ≥ 2 closes above L with CVD and OI rising | veto | Osler: stop cascades trend fast |
| 3 | Taker imbalance | `TI = (Vbuy − Vsell)/(Vbuy + Vsell)`; 1h TI z-score > +2 | veto | Cont-Kukanov-Stoikov OFI (PR); crypto OFI predictive but short-lived (WP) |
| 4 | Absorption / CVD divergence | price makes a higher high, CVD a lower high; net-buy z > 2 with the move < 25% of predicted | bearish only at a swept level | PRAC |
| 5 | Wall flicker / spoofing | a size ≥ 5–10× the median level, ≤ 10bp from mid, vanishing without trades, lifetime < 5–30 s | ignore walls; veto on vanishing asks | WP (Bitfinex/Kraken/Coinbase L3); Binance only offers L2 |
| 6 | Fake volume | Benford first digit (Nigrini MAD > 0.015), round-lot clustering | down-weight volume/CVD signals | Cong et al. 2023, Aloosh & Li 2024 (PR, spot) |
| 7 | Perp-led vs spot-led | `basis_bp = (perp − spot)/spot·1e4`. **Perp-led:** perp return > spot return, basis widening +10–20bp, flat spot TI. **Spot-led:** spot TI > 0 and basis ≤ 0 | perp-led = fade candidate; spot-led = veto | leadership mixed (PR/WP); perp-spot gaps mean-revert (He-Manela WP) |
| 8 | Crowding | price up, OI up ≥ 10%/24h, funding ≥ 0.03%/8h (fade after a turn). Squeeze = funding < −0.01% with OI and price rising | setup / veto | carry-crash at monthly horizons (PR); funding premium mostly eaten by costs (PRAC) |
| 9 | Liquidation exhaustion | long-liq burst z > 3, OI −2–5% in 5–15m, volume > 10× baseline | veto fresh shorts (overshoot, then buyback) | Brunnermeier-Pedersen 2005 (PR); Oct 10 2025 (WP). WS-only, undercounts (1 order/s/symbol) |
| 10 | Pump-and-dump | 1h: price > +5% and volume > +300% vs 12h baseline (strict: 24h, 10%, 400%). Rush orders mark the pump | veto during; short after rush orders stop **and** price < pump VWAP | Kamps & Kleinberg 2018 (PR); La Morgia (WP) |
| 11 | Chart patterns | encode sweeps/wicks as code; eyeballing is not evidence | the chart is for review, not the signal | Lo-Mamaysky-Wang 2000 (PR) |

**"Someone sees all orders."**
- **Documented:** CFTC 2023 alleged about 300 undisclosed Binance house accounts. The SEC alleged wash trading on Binance.US. On Oct 10, Binance's internal pricing depegged USDe, wBETH and BNSOL, and Binance paid $283M in compensation.
- **Not documented:** an exchange deliberately running specific users' stops.
- **Needs no hidden data:** stop and liquidation levels can be inferred from public leverage, OI and round numbers (Osler; predatory trading).

**What we must validate:**
- Forward returns 15m–24h for signals 1, 4 and 7.
- Cost drag.
- The false-veto rate of signals 3 and 8.

Key sources: Osler JF 2003 · Cont-Kukanov-Stoikov JFEC 2014 · Cong et al. MS 2023 · Kamps & Kleinberg 2018 · Brunnermeier & Pedersen JF 2005 · Schmeling-Schrimpf-Todorov (BIS WP 1087) · He-Manela-Ross-von Wachter (arXiv 2212.06888) · Lo-Mamaysky-Wang JF 2000 · Urquhart 2017.

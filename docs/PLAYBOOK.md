# Short Playbook (research-backed, 2026-09)

Operational playbooks per setup live in `.claude/skills/` (pump-fade, listing-fade, delisting-short, unlock-short). This file is the research background with sources. See `docs/RESEARCH.md` for how the rules evolve. Every other strategy we know of (long, short, neutral; not yet traded) is in the library, `docs/strategies/`, with industry benchmarks in `docs/INDUSTRY.md`.

Tags: **[W]** widely used by practitioners, **[S]** single source, **[Mine]** our own synthesis. These are starting rules; the journal decides what we keep.

## 1. Scan (5–10 candidates per session)
- Universe: Binance USDⓈ-M **perps**, sorted by futures 24h % change. Spot gainers often have no perp yet.
- Gate: 24h change ≥ +10% [S, FMZ]. Score more heavily above +30%.
- Liquidity: OI ≥ $30M [S] and 24h quote volume ≥ $20M (hard limit in code).
- Froth score inputs:
  - relative volume ≥ 3× [W]
  - funding ≥ +0.05%/8h elevated, ≥ +0.10% extreme, normalised by funding interval [W]
  - OI up sharply alongside price [W]
  - 1h RSI > 80 [W]
  - price stretched far above VWAP or EMA [W]
  - listing age < 30 days [W]
  - taker buy/sell ratio falling while price makes new highs [W].
- Weight the fade toward mid and small caps. **Do not fade majors breaking out**: large caps show momentum, small caps show reversal.

## 2. Veto (any one = no short)
1. Funding negative, or the 1h capped interval: shorts pay. Since 2026-09-29 this is a FLAG, not a veto: half size plus a funding budget that exits at 0.5R paid without follow-through. Round 3 (learning #22) found the blocked trades did better and the funding paid was small [W → tested]
2. OI rising while funding is negative and price is flat or up [W]
3. Funding at its cap, or the symbol has moved to 1h funding (`fundingInfo`): a flag like 1, not a veto [W]
4. Low float / Binance Alpha tokens: MYX ran +1,400% and liquidated $89.5M of shorts [W]
5. Funding that is an extreme outlier on a low-cap coin (a "gamed" squeeze setup) [S]
6. OI < $30M, a thin order book, or a fresh bullish catalyst in the news [W]
7. BTC breaking out, or an altseason regime: shorting top gainers takes streaks of losses in strong uptrends [W]

## 3. Entry trigger (wait for confirmation, never short blindly)
- A lower high after the pump, or a loss of the prior day's close ("first red day") [W]
- A rejection wick into the +2/+3 SD VWAP band: enter on the break of that candle, stop above the wick [W]
- A failed retest of VWAP or EMA20 from below on 15m/1h [W]
- Taker/CVD or RSI bearish divergence [W]
- Safer variant: after a roughly 50% retrace, short the bounce [S]
- Positive funding is a tailwind, **never a trigger on its own** [W]
- Weekend: thinner order books, so use smaller size [Mine]

## 4. Sizing and stops
- Risk 0.5–2% of equity per trade (the `.env` baseline and the desk limits set it; demo experiments move it within the human's bounds). Size = risk ÷ stop distance [W].
- Stop above the swing high plus a 1.5–3× ATR buffer [W]. Isolated margin, 1–3× leverage, a reduce-only stop-market on the exchange.
- Low leverage protects better than a tight stop, because stops slip in squeezes [W].
- **Never average up into a losing short** [W; FMZ's inverse pyramid is rejected].

- These numbers are research parameters. The analyst can test other values (e.g. 5× leverage with a tight stop) with
  `set_risk_params` and a named experiment, and judge them by `journal` → `by_experiment` [Mine].

## 5. Manage and exit
- Take partial profit into the first flush and trail the remainder [W]. Targets: the pre-pump base, or a 0.5/0.618 retrace [Mine].
- Cut immediately when:
  - price reclaims the invalidation level
  - funding turns deeply negative while OI rises
  - funding bleed exceeds budget [W]
- Time stop: the setup's default (pump_fade 12h, setups.py), aiming at the 75th percentile of time-to-MFE (POSITION_MANAGEMENT.md) [Mine].
- After a tagged delisting (`deliveryDate` set) there is usually a fast dump, but expect squeezes [S].

## 6. Portfolio
- Total open risk ("heat") at most 5–8%. Correlated alt shorts count as **one** BTC-beta bet [W].
- Watch BTC: it leads alts. Pause new shorts on a BTC breakout [Mine].
- ADL risk: Binance can force-close profitable shorts in a crash (10 Oct 2025) [W].

## Key sources
- FMZ pump-short workflow: https://dev.to/quant001/dont-panic-when-coins-pump-workflow-powered-shorting-to-catch-the-pullback-31g2
- Robot James, shorting perp listings: https://robotjames.substack.com/p/shorting-perp-listings-on-binance
- Tradingriot, crypto data: https://education.tradingriot.com/cryptocurrency-data/
- Kraken funding strategy: https://www.kraken.com/learn/futures-trading-funding-rate-strategy
- Binance Academy, short squeeze: https://www.binance.com/en/academy/articles/what-is-a-short-squeeze
- Reversal vs momentum by size: https://www.sciencedirect.com/science/article/pii/S1057521921002349
- Delisting shorts (FMZ): https://blog.mathquant.com/2026/05/18/shorting-binance-delisted-perpetuals-a-grid-strategy-from-monitoring-to-auto-execution.html
- Listing-fade study: https://github.com/Asalio123/binance-listing-study · Unlocks: https://keyrock.com/from-locked-to-liquidity-what-16000-token-unlocks-teach-us/

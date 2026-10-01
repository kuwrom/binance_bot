# Time, sessions and the warm-up (research 2026-09-28)

Evidence labels: [S] strong/replicated, [M] one solid study, [W] weak or folklore, [P] official docs, [U] unverified.

## What time predicts
- **Volatility and volume patterns are robust; hour-of-day return effects are not.**
  - Baur et al. 2019, FRL, 15M observations: return anomalies don't persist; activity is lower in local evenings and at weekends. [S]
  - Hansen-Kim-Kimbrough (arXiv 2109.12142), Binance and Coinbase:
    - Volatility peaks around 16 UTC and bottoms near 05 UTC.
    - Spikes at 00, 08 and 16 UTC, the funding times.
    - The first hour of the UTC day is the most volatile, especially on Mondays. Weekends are quiet, Saturday the quietest.
    - Volatility bursts at minute :00–:01 of each hour, smaller ones at :15/:30/:45. The authors attribute these to algos.
    - These patterns are getting stronger. [S]
  - Eross et al. 2019: volatility peaks at the stock-market opens; liquidity is thinnest in the early UTC morning. [M]
- **Intraday momentum and reversal both exist** (Shen-Urquhart-Wang; Wen et al. 2022), and they flip after price jumps and on FOMC days. [M]
- **Day of week:**
  - Long et al. 2020: across 151 coins, a coin's past returns on the same weekday predict its future ones. [M]
  - A Monday effect exists in BTC only. [W]
  - "Asia pump / US dump" has no rigorous support. [W]
- **Verdict:** use time for execution, stop width and size. Never use it as a short trigger unless our own data supports it.

## Clocks
- **Funding:**
  - Default settlement is 00/08/16 UTC.
  - Since 2025-05-02, a symbol whose funding hits its cap or floor switches to 1h funding.
  - Since 2026-01-02, it reverts to 4h after 16 periods below |0.025%|.
  - You pay only if you hold at the timestamp (15-second grace). The interval comes from `/fapi/v1/fundingInfo`. [P]
  - Behaviour around funding: only the volatility and volume spikes are documented. No directional effect is proven. [U]
- **US macro:**
  - CPI, NFP and PCE: 08:30 ET (12:30 UTC in DST, 13:30 UTC in winter).
  - FOMC: 14:00 ET, press conference 30 minutes later.
  - BTC falls about 24 bp per 1-standard-deviation CPI surprise (Pinchuk 2023). [M]
  - Volatility spills over after FOMC (Corbet 2020). [M]
  - Alts have higher beta than BTC, so expect bigger moves.
- **Deribit expiry:** 08:00 UTC, mostly Fridays, the same time as the 08:00 funding. [P]
- **Daily and weekly close:** 00 UTC and Monday 00 UTC fall inside the most volatile hour. [M]
- **US equity open:** 09:30 ET, the nearest crypto equivalent of an "open" (opening-range research, Zarattini & Aziz). [M]
- **Free calendars:**
  - BLS iCal (exact times; needs a real User-Agent).
  - The Fed FOMC page (2026 remaining: Oct 27–28, Dec 8–9).
  - The ForexFactory weekly JSON (unofficial: cached, backup only).

## Rules in code
- **Hard vetoes on new entries** (`signals/session.py`):
  - Tier-1 macro (CPI/NFP/PCE): from 15 minutes before to 30 minutes after.
  - FOMC: from 30 minutes before to 60 minutes after.
  - Maintenance: ±30 minutes.
- **Execution only:** no market entries within ±2 minutes of funding or in minute :00–:01 of an hour.
- **Soft:** during the 00/16 UTC hours, a Deribit Friday 08:00, or the weekly close, use wider stops or smaller size (features only).
- **Research features:**
  - hour, session, weekday, minutes to funding, funding interval
  - minutes to the daily and weekly close, US-open window, Deribit flag
  - macro proximity, minute of the hour.

## Warm-up (cold desk only)
- **Why:** a checklist only helps when it is short, enforced and actually used.
  - WHO surgical checklist, Haynes 2009: mortality fell from 1.5% to 0.8%.
  - Ontario mandate, Urbach 2014: no effect.
  - No crypto study measures what a 15-minute delay is worth, so we measure it ourselves.
- **Cold** means no trade activity in the last 6 hours, or the last activity was on an earlier UTC day. When the desk is cold, entries need a completed warm-up (or a `veto_override` with a reason).
- **Protocol** (`shortbot warmup start|check`):
  - Take a snapshot at t0 and another at t+15, at a non-round minute.
  - Snapshot contents: last price, mark, index, basis, funding rate and interval, OI, spread, depth within ±1%, 1m realised vol, BTC 15-minute return.
  - Drop a candidate if any of these happen:
    - a new local high
    - OI up more than 3% while price rises
    - funding flips negative or switches to 1h
    - spread more than doubles, or depth halves
    - BTC up more than 1%
    - a macro window now overlaps the entry
  - These thresholds are starting assumptions, to be tuned by the persistence study.
- **Persistence study:** every warm-up logs both snapshots. Compare persisted vs dropped, and the t0 vs t+15 fill, on net R at 1h, 4h and 24h, using a day-clustered bootstrap. Keep the delay only if the CI is above 0 with at least 100 candidates per setup.

# Coin controller profile (research 2026-09-28)

Each coin plays its own game. We can't see who controls it or what they intend. We can see their constraints (float, unlocks, listing age) and their footprints (volume vs depth, perp vs spot, funding, OI), and turn those into probabilistic labels. Labels shift odds and size. They never trigger a trade on their own.

Tags: [S] strong/replicated, [M] one solid study or court record, [W] weak/folklore, [P] official docs/practitioner.

## Evidence
- **Wash-trading market makers are proven:**
  - SEC, Oct 2024: Gotbit, ZM Quant and CLS Global ran "market-manipulation-as-a-service" for token promoters. [M] https://www.sec.gov/newsroom/press-releases/2024-166
  - WSJ, May 2024: Binance surveillance found ~$300M of DWF Labs wash trades (YGG and others). [M]
  - Footprint: volume far above book depth, repeated trade sizes, a flat daily volume profile, little price impact.
- **Market-maker token-loan + call-option deals** reward pumping above the strike and then selling. [P/W] MOVE 2025 (a market maker sold borrowed tokens after the listing): *unverified*.
- **Low float and high FDV:** most upside is priced in before listing, with ~$155B of unlocks due 2024–2030 (Binance Research). [P]
  - ~90% of unlocks come with negative pressure, and team unlocks are the worst (Keyrock, already used in `market/events.py`). [P]
- **Pump-and-dump targets** are small, illiquid coins with sudden volume bursts. [S] Sources: Xu & Livshits (412 pumps), La Morgia et al. (~900 events, rush orders), Kamps & Kleinberg.
- **Weak footprints** [W]: a one-sided book, price pinned to round levels, the same move at the same hour every day, OI running ahead of spot volume, funding stuck negative.

## Free data
- **CoinGecko Demo:** supply (circulating/total/max), categories and contracts. 100 calls/min, 10k calls/month, attribution required. A weekly refresh of ~460 coins costs ~2k calls/month.
- **Binance public endpoints:**
  - perp vs spot 24h volume
  - `/fapi/v1/depth` (depth within ±2%)
  - OI history and funding
  - `onboardDate` (listing age)
  - product `tags` (Monitoring, Seed) and `cs` (circulating supply). Undocumented; weekly only.
- **Not free:**
  - holder lists (Etherscan PRO; BSC and Base are paid)
  - DefiLlama emissions ($300/month)
  - Bubblemaps, Nansen; Arkham is by application.
  - So we use float as a proxy for holder concentration. Unlocks stay manual, entered with a source.

## Labels (thresholds are assumptions, fixed before testing)
| Label | Rule | For a short |
|---|---|---|
| Insider-supply overhang | float < 35% and FDV/MC > 2.5, plus a team/investor unlock ≥ 2% within 30 days, or listed < 180 days | Favour. Enter 14–28 days before the unlock, exit around it |
| Thin float, squeeze-prone | float < 20%, or OI/MC > 0.3, or perp/spot > 5, or depth ±2% < $150k, or 7-day funding < 0 | Avoid, or ≤ ¼ size after a confirmed turn. Stops beyond the pump high. Negative funding blocks |
| MM-managed range | daily volume CV < 0.3, volume/depth > 50×, realised vol in the bottom tercile, strong hour-of-day pattern | Fade the range top only, ½ size, small targets. A range break means exit |
| Organic | none of the above; float > 60%, listed > 365 days | Standard |

**Overlay:** a Monitoring tag sends the coin to the delisting-short playbook, at ½ size.

## Against anecdotes
- Pull per-coin statistics toward the universe average (profile.py uses w = n/(n+30)).
- A coin's own statistic is not used below n = 10 events or 60 days.
- Change a label's thresholds only with ≥ 30 trades behind that label.
- Treat dramatic cases (MOVE, YGG) as proof that a mechanism exists, not as base rates.

## Unknowable
- who controls the coin, their cost basis, the market-maker contract terms, OTC sales, and what they intend
- whether an unlock recipient sells, hedges or holds.

## Implementation
- **Lifecycle signal:** `signals/lifecycle.py`. Stages: delisting_risk > new_listing > unlock_ahead > pumping > post_pump > bleeding > young > mature.
- **Controller signal:** `signals/controller.py`. Labels: thin_float, insider_overhang, mm_range, organic, unlabelled. Supply facts come from a weekly CoinGecko cache (`market/supply.py`).
- **Logging:** both signals are logged with every scan candidate. The research report groups outcomes `by_lifecycle`, `by_controller`, `by_archetype` (stage × tape game; "unclassified" when no script fits) and `by_session`.
- **`shortbot read COIN`** (`read.py`): the verdict, plus layered evidence from all coins → archetype → this coin, shrunk by n/(n+30).

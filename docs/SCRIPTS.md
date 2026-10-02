# Market scripts catalog: why the biggest moves happened (forensics 2026-07-31 → 09-29)

Per-episode data (tape features, catalyst, URLs, labels, causal short outcome): `data/scripts_catalog.json`.
Code: **lost.** The forensics ran from a session scratchpad (`forensics/`) that no longer exists, so the [U] universe test (303 +35% crossings, 72h net of funding, the premium / funding / weekend splits) and the per-episode feature extraction can't be rerun as computed. What remains:
- the per-episode data above, which `registry_seed.seed_scripts` loads as `script.*` patterns with hindsight-only `forensics` episodes;
- in-package re-tests of the hypotheses on hist.db: `backtest/strategies_v2.py` (F4 discount, F5 weekend, the retail flip; [failures/ROUND1.md](failures/ROUND1.md), whose OOS is contaminated for F4) and `backtest/strategies_v4.py` (the veto contradictions; [failures/ROUND3.md](failures/ROUND3.md)).

Treat the [U] numbers below as unreproducible until they are rebuilt as a stamped backtest (docs/BACKTEST.md "Provenance").

**Evidence tags.**
- **[T]** tape fact: measured on Binance data.
- **[C]** public catalyst fact, with a URL in the JSON. Most sources are aggregators or AI write-ups, so treat them as leads.
- **[I]** our inference.
- **[U]** universe test: every 24h > +35% "pump crossing" in 60 days. 303 events on 182 coins, causal entry at the crossing hour, 72h outcome net of funding.

**Caveats.**
- This covers one 60-day regime: BTC rose about 20–24% in August, and the median alt rose 10–15%.
- The 49 episodes were picked by size, which is hindsight. Only the [U] tests are causal.
- Nothing here clears the |t| > 3 plus later-week-repeat bar in `docs/PATTERNS.md`. Every rule below is a hypothesis to register.

## Data
- **Universe:** 535 USDT-M perps, including 8 delisted in the window.
- **Hourly data:** klines, premium index, funding, spot klines.
- **data.binance.vision daily `metrics`**, 5-minute series:
  - `sum_open_interest_value`
  - `sum_toptrader_long_short_ratio`, the top-trader **position** ratio: whales
  - `count_long_short_ratio`, the global **account** ratio: retail
  - `sum_taker_long_short_vol_ratio`
- **OI history:** 30 days of 1h OI for all perps.
- **Announcements:** 352 Binance announcements (CMS API).
- **Web:** catalyst search for about 30 coins.

## Headline
1. **The biggest pumps are short squeezes of retail, not long manias.** [T]
   - In 17/42 pump episodes, the retail account ratio collapsed during the pump. Typical moves were 1.3–3.9 → 0.3–0.7: retail shorted into the pump.
   - Meanwhile OI in contracts rose 50–370%, funding stayed ≥ 0 and top-trader positions stayed net long.
   - The top wallets hold 80–99.8% of supply in most of these coins [C].
   - **The dump starts when retail flips back to long.** HEI 0.49→1.31, IOST, ACE, SOPH, AIN, Q, TAKE.
   - When retail stays short, the pump keeps running. BR, 龙虾, CYS, BTR and BULLA went +45% to +100% more.
2. **Perp-discount pumps fade; perp-premium pumps don't.** [U, both halves of the window]

   | Premium at the +35% crossing | n (coins) | Result, 72h net of funding | p90 adverse move |
   |---|---|---|---|
   | ≤ −1% | 26 (24) | **+11.3% mean, 85% win** | 33% |
   | > +0.3% | 33 | −6.2% | 131% |
   | All pumps | 303 | +0.1% | 109% |

3. **Negative-funding / capped-funding pumps faded better than average.**
   - [U] Funding ≤ −0.1%: +7.5% net, 79% win (n=34).
   - The cost is real. Shorts paid a median 4.5% in funding over 72h in discount pumps, up to 27% (LSK). ONG −19% and SKR −20%: funding turns a win into a scratch.
4. **Weekend crossings fade and weekday crossings squeeze.** This confirms learning #6.
   - [U] Weekend: +8.8% net, 78% win, p90 adverse move 60% (n=91).
   - [U] Weekday without a discount: −5.1% net, 42% squeeze further than 30%, p90 adverse move 127% (n=193).
5. **Naive fading of the largest moves is ruinous.** Adverse move from the +35% crossing:

   | Coin | Adverse move |
   |---|---|
   | LSK | +1,389% |
   | ONE | +386% |
   | AKE | +378% |
   | BTR | +341% |
   | TUT | +314% |

   - [U] 37% of all crossings ran more than 30% further before turning.
   - The median crossing still ended 12% lower after 72h. The edge lives in the median; the mean is killed by the tail.

## Frequency table (49 episodes; one episode can carry several labels)
| Script | Primary | Any label | Registry state today |
|---|---|---|---|
| retail_short_fuel_pump | 17 | 17 | none (closest: `crowded_longs`, but the crowd is **short**) |
| abandonment_collapse | 10 | 12 | `bleeding` lifecycle (no tape state) |
| discount_squeeze (funding farm) | 8 | 12 | vetoed by the funding<0 / spot_led rules |
| delisting_squeeze | 4 | 4 (+3 small delistings in the [U] check) | delisting-short playbook assumes a dump |
| macro_beta_squeeze | 3 | 4 | regime veto (BTC breakout) |
| venue_listing_pump | 0 | 6 | none |
| transfer_freeze_squeeze | 2 | 3 | none |
| pre_unlock_exit_pump | 0 | 3 (+1 counter-example) | `pre_unlock` (expects a bleed, not a pump) |
| narrative_lag_pump | 1 | 3 | none |
| binance_spot_listing_frontrun | 2 | 2 | none |
| unlock_bleed | 1 | 2 | `pre_unlock` ✔ |
| event_convergence (token merge) | 1 | 1 | n/a |

## Catalog

### 1. retail_short_fuel_pump — "low-float perp pump fed by retail shorts" (17)
**Tape.** [T]
- Perp-only on Binance, or thin spot. A no-news pump of +100% to +600% in about 24h.
- Global account L/S falls by ≥ 0.4 during the pump, to below 0.8.
- OI contracts (OI value ÷ price) rise ≥ 50%.
- Funding stays ≥ 0 and the premium is ≥ 0. Retail is short by count but not by size: whales hold the long.
- In 10 of 17 episodes the biggest hourly bar fell in Asia (00–07 UTC) or on a weekend.
- Taker buy share is only 0.50–0.52: price moves on thin books, not on aggressive buying.

**Catalyst.** None, or a trivial one. 9 of 17 had no public catalyst. The others were a minor perp-venue listing (Aster, Websea), a trading campaign, or Korean-exchange flow (BTR/Bithumb).

**How it ends.** [T]
- Retail flips back long: global L/S rebounds by ≥ 0.3 to above about 1.2 within 12–48h. The dump follows: −40% to −75% within 24h (HEI, TAKE, AIN, Q, SOPH, IOST).
- If retail never flips (BR 0.29, 龙虾 0.41, CYS 0.55, BTR 0.36), the pump keeps going for days.

**Episodes.** Aug: BTR 08-26, BTR 09-01, TUT 08-09, HEI 08-06, BMT 08-09, APR 08-12, CYS 08-05, KOMA 07-31, BLESS 08-05, AKE 08-14. Sep: 龙虾 09-12, TAKE 09-23, BULLA 09-05, BR 09-17, AIN 09-15, Q 09-27, SYN 09-16.

**Short, and the risk.** [U]
- Shorting at the crossing when retail shorted into the pump (global L/S −0.3 in 24h): median +15.9%, but mean only +2.2% with a p90 adverse move of 112% (n=135).
- **The flip entry** (short the first hour global L/S rebounds ≥ 0.3 off its post-crossing low) cuts the squeeze rate from 35% to 25% and the p90 adverse move from 112% to 89%. The mean stays about the same (+2.7%). It is a risk filter, not an alpha source.
- **No flip within 72h is the danger case:** −14% mean (n=23), so exit or never enter.
- Stop above the pump's wick high. The wick bar is often 5–50% above its close (LSK 51%, TUT 43%).

**Confidence.** Medium on the mechanism. The flip filter is low-to-medium: one regime, same-window test.

### 2. discount_squeeze — "crowded-short funding farm" (12)
**Tape.** [T]
- A pump during which the **perp trades below the index**: premium ≤ −1%, reaching −5% to −42% (ONG −42%, ACE −20%, LSK −19%).
- Funding hits the **−2% cap** and Binance switches the symbol to a **1h** interval (LSK, ONE, AKE, SKR, IOST, ONG, SOPH, G, BANK).
- OI often builds **before** the pump: IOST +253%, LSK +566%, SOPH +119%, QNT +347% in 72h.
- During the blow-off, OI contracts may fall: LSK −35%, shorts liquidated.
- Spot usually moves in the same hour as the perp. Spot is the index, so price is set where shorts can't arbitrage.

**Catalyst.** A stale narrative (LSK restructuring 3 weeks earlier; IOST, a $50k burn) or a transfer freeze (script 5). An OKX Orbit post called IOST a whale squeeze via negative funding [C].

**How it ends.**
- A wick blow-off, 50% retraced within 1–7h, then −50% to −85% within 7 days (LSK, IOST, SOPH, ACE).
- The controller profits twice: from the squeeze and from capped funding. Shorts paid 19–27% in funding over 72h on LSK, ONG and SKR. [I]

**Short, and the risk.** [U] At a premium ≤ −1% at the crossing: +17.8% gross / +11.3% net, 85% net win, p90 adverse move 33% (n=26, 24 coins; Aug 94% win, Sep 90%).
- Budget funding explicitly: exit if the 1h cap persists more than 12h without price follow-through.
- Size small. LSK went +1,389% from its crossing: the discount was only −0.9% then, so it did not qualify.

**Confidence.** Medium: repeated in both months, n=26.

### 3. abandonment_collapse — "pump-and-abandon / whale float dump" (12)
**Tape.** [T]
- −50% to −88% in under 30h with no catalyst (VELVET, BANK, AIN, APR, BTR), or a −70% to −97% bleed over 30 days (BEAT, VELVET, CYS, TUT, STAR, US).
- OI value falls 34–84% over the 30 days.
- Daily volume shrinks 5–17×: liquidity is withdrawn.
- 58–71% of days are red.
- Violent dead-cat pumps along the way: TUT had 80 hours with a 24h return above +20%, BEAT 62.
- Retail **buys** the collapse: global L/S rises to 1.8–2.4 (VELVET, BANK, APR) while contracts rise.
- These are often perp-only coins whose top 100 wallets hold 98–99.8% [C]. It is idiosyncratic: STAR fell −74% while the median alt rose +15%.

**Short, and the risk.**
- Short the dead-cat pumps: the retail-flip entry, or a first red 4h bar after a +20% bounce.
- A continuation short after the crash worked for VELVET and BEAT. After APR's −72%, price bounced +34% within 24h.

**Confidence.** Medium on existence, low on timing.

### 4. delisting_squeeze (7 of 7 delistings)
**Tape.** [T] Every perp delisted in the window rallied after the notice:

| Coin | Max above the announcement price | Result at settlement |
|---|---|---|
| HFT | **+476%** | +206% |
| VIC | +128% | ~0% |
| STORJ | +45% | ~0% |
| SCRT | +43% | −41% |
| ACX | +42% | ~0% |
| ICX | +21% | ~0% |
| VANRY | +10% | −15% |

- The announcement hour itself moved only −6% to +3%.
- HFT's pattern: retail accounts shorted (global L/S 1.66 → 0.31) while top-trader positions stayed long (1.1–1.4). Funding was **positive** (up to +0.86%). The perp printed a **+51% premium** over spot in the final hours before settlement.

**Catalyst.** Binance delisting notices of 08-03 and 08-20 [C].

**Short, and the risk.** The median outcome at settlement is roughly flat, and the median squeeze is about +42%.
- Shorting the notice has no edge, and its tail is the worst of any script.
- The only safe form: after a ≥ +40% post-notice squeeze, with the retail flip, at tiny size, and flat 24h before settlement.

**Confidence.** Medium (7/7, one quarter).

### 5. transfer_freeze_squeeze (3)
**Tape.** [T] A pump while deposits and withdrawals are suspended for a hard fork or migration. The perp sits at a deep discount and funding is at the −2% cap.
- ONG: the hard fork on 08-21 with Binance and Upbit suspensions [C].
- SOPH: migration to Base, Upbit suspension 09-08..11 [C].
- ONE: network sunset/migration, plus Binance Last-Price-Protection ending 09-17 13:54 mid-pump [C].

**Mechanism.** [I] Arbitrageurs can't move coins, so spot can be walked up while perp shorts are trapped.

**How it ends.** The dump comes when transfers reopen or retail flips: ONG −46% and SOPH −56% within 24h.

**Short.** Short after the freeze ends. An announced freeze is a **veto** until then.

**Confidence.** Low-medium (n=3, clear mechanism).

### 6. venue_listing_pump (6, a catalyst tag)
**What happens.** The pump starts the day a *new perp venue* lists the coin: Aster (TUT 08-06, LSK 09-13), Websea (BULLA, MUBARAK), Bitget/OKX/BTCC (AKE 09-02, 09-16, 09-19), Bitget PoolX (ACE).

AKE pumped 5 times by more than 60% in 60 days, once per new venue. It dumped −50% on its 9.35% unlock day (09-21).

**Use.** A new-venue listing is a **veto for about 24h**, and a flag that the float is being run.

### 7. binance_spot_listing_frontrun (2)
- **MARSCOIN:** the perp rose +200% in the 36h **before** Binance announced its spot listing (Seed tag) on 09-04 10:15.
- **牛来:** a −46% sell-the-news within hours of the 09-09 spot notice, then a second +116% pump with retail short and whales long.
- **Both:** Binance later announced holder airdrops (SpaceX/QQQ bStocks, 09-28).

**Use.** On perp-first Seed-style coins, don't short before the spot decision. Short the spot-notice spike.

### 8. pre_unlock_exit_pump (3 + 1 counter-example)
**What happens.** A pump 1–3 days **before** a team or ecosystem unlock, then a dump into it:
- TAKE: +234% on 09-23, unlock 09-25, −70% on 09-24.
- AKE: unlock 09-21 = dump day.
- ACE: +189% on 08-14, unlock 08-18.

**Counter-example.** BR rose through its 09-20 unlock (+60%).

**Relation to our pre_unlock setup.** Our `pre_unlock` state expects a bleed. That holds for large liquid coins (KAITO −72%, most of it before the 08-20 unlock, with no relief rally above 20%). On whale-float coins, expect a **pump into the unlock** first.

### 9. macro_beta_squeeze (4)
**What happens.** A market-wide short liquidation lifts high-beta memes: TRUMP, PEOPLE, MELANIA and NEIRO on 08-21/22, when there were about $3B of liquidations and BTC rose ~+10% [C]. MUBARAK on 09-22 ($919M of liquidations).

**Tape.** The only script where retail stayed long: global L/S ≥ 1.1 (TRUMP 2.1).

**Short.** The fade worked with a small tail: TRUMP +10% after 72h, adverse move 35%; BTC above +3% in 24h: 71% win, p90 adverse move 56% [U].

### 10. narrative_lag_pump (3) and event_convergence (1)
- **narrative_lag_pump:** real news that is 2–21 days stale, pumped on a weekend or in Asian hours. QNT: the Clearing House news came out 09-24 and the pump ran on Sunday 09-27 (+141%, then −38% within 24h). LSK and IOST also fit. Treat these like the discount squeeze.
- **event_convergence:** STG was pinned to ZRO by a token-merge ratio. It is not tradable as a fade.

## Contradictions with the current registry
1. **The `spot_led_rally` stand-aside and PLAYBOOK vetoes 1, 2 and 5 (negative funding) are contradicted.** Spot-led pumps with the perp at a discount and negative funding were the *best* fades (85% net win), not squeeze fuel.
   - What those vetoes should really key on: funding at the cap on a 1h interval that persists without a price turn (the cost), and a transfer freeze.
2. **TAPE #7 "perp-led = fade candidate" is contradicted.** A perp premium above +0.3% at the crossing lost 6% on average (p90 adverse move 131%).
3. **`crowded_longs` misreads these pumps.** The crowd in the biggest pumps is **retail short**. We need a `retail_short_fuel` state: global account L/S below 0.8 and falling while OI contracts rise. Stance: wait for the flip.
4. **The delisting-short playbook** ("usually a fast dump") is contradicted: 7 of 7 squeezed after the notice, with a median maximum of +42% and HFT at +476%.
5. **TAPE #9 liquidation-exhaustion veto (weak).**
   - Long flushes (price −20% or more and OI contracts −20% or more within 12h, n=14, last 30 days) kept falling: median −16% after 72h, 57% lower.
   - Squeezes (price +25% or more with OI contracts −15% or more within 12h, n=31) reversed: median −23% after 72h, 77% lower.
6. **`pump_fail` (first red close) is confirmed as weak.** The retail flip is the better timing filter.

## Negative results (don't build on these)
- **Whale-vs-retail divergence** (top-trader position L/S at least 0.6 above the global ratio) at the crossing did not separate outcomes. n=65: +2.8%, squeeze rate 40%, against about +1% overall.
- **Binance Alpha trading competitions** are not a script. After 13 announcements on coins with perps, the 14-day median was about −3%. Three collapsed: APR −58%, COLLECT −80%, DOS −34%. Three rallied more than 50%: PRL, CAP, PIEVERSE.
- **Monitoring-tag coins pumped rather than bled.** SOPH rose +154% and RARE +88% after their tags (08-11). The tag is not a short trigger on its own.

## Codable next steps (hypotheses; register in the trials log)
1. **Premium and funding interval at the event hour.** Add `prem_at_event` and `funding_interval_h` to `signals/tape.py`, and a `discount_squeeze` state with stance `fade`, sized small and budgeted for funding.
2. **Global account L/S history.** Add `glob_acc` and `glob_acc_min_since_event` (metrics / `globalLongShortAccountRatio`), plus a `retail_short_fuel` state (stand aside) and a `retail_flip` trigger.
3. **Delisting risk.** A delisting notice should become `stand_aside` until a ≥ 40% squeeze plus the flip. Flat 24h before settlement.
4. **Event vetoes.** Veto for 24h on a new external perp listing or an announced transfer freeze (needs a news feed), and on a Binance spot-listing decision still pending for a Seed-style perp.
5. **Re-run weekly.** Keep only the cells that repeat (PATTERNS.md rules).

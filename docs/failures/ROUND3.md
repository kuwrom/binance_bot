# Round 3 (2026-09-29): are the live vetoes right?

The forensics catalog (`docs/SCRIPTS.md`, "Contradictions with the current registry") claimed that several live vetoes look backwards in this 60-day window. Round 3 tests each one as a **partition of a base book**:
- **blocked:** the base signals the veto fires on. This is what inverting the veto would trade.
- **allowed:** the base book with the veto on. This is what the desk trades.

A veto is right if its blocked trades do worse. Bases: pump_fade (1 ATR, 12h), pump_fade+discount, and level_fade (round, market, 24h), the sweep trigger. Code: `backtest/strategies_v4.py`, `backtest/report_v4.py`, `--round3`, `tests/test_backtest_v4.py`.

### 1. Diagnosis (in-sample only: entries before the 2026-08-29 19:30 UTC midpoint)
Blocked vs allowed on the IS trades of each base, avg R net of costs (n in brackets):

| veto (live rule) | pump_fade (IS n 1097) | pump_fade+discount (64) | level_fade (4458) |
|---|---|---|---|
| premium > +0.3% | −0.13 [29] vs −0.09 | −1.01 [1] vs +0.09 | −0.11 [43] vs −0.16 |
| perp-led (a +5 bonus live, not a veto) | +0.14 [20] vs −0.09 | +0.00 [8] vs +0.08 | −0.07 [72] vs −0.16 |
| negative funding or 1h interval | −0.04 [137] vs −0.10 | +0.11 [51] vs −0.06 | **+0.01 [493] vs −0.18** |
| spot-led rally | **−0.23 [99] vs −0.08** | +0.16 [3] vs +0.07 | −0.22 [329] vs −0.15 |
| long flush in the last 3h | +0.09 [29] vs −0.09 | −0.10 [8] vs +0.10 | **+0.27 [98] vs −0.17** |
| taker-buy burst | −1.16 [1] vs −0.09 | – [0] | −0.23 [75] vs −0.16 |

Premium buckets on IS pump_fade: ≤ −1% +0.03 [19], −1..0% −0.12 [845], 0..+0.3% +0.02 [204], > +0.3% −0.13 [29].

IS event counts (all tradable coins, OI contracts −3% in the hour): long flush (1h move ≤ −2 ATR) 437; squeeze (≥ +2 ATR) 46. At 1.5 ATR the counts are 641 / 75, and at 3 ATR 231 / 18. The threshold stays at **2 ATR**, as written in the code before the counts: squeeze events are rare at any stricter bar.

**What the IS diagnosis says:**
- The negative-funding and flush vetoes block trades that did *better* in IS. The spot-led veto blocks trades that did worse.
- The live taker-burst veto almost never fires on these triggers. A failed-sweep entry bar is rarely a green bar with a TI spike, so it is close to inert here. Round 1's broader taker veto (F3) was already rejected.
- The premium > +0.3% cell is small (29 / 43 IS trades), so the forensics claim ("the worst fade") has little IS support either way.

### 2. Pre-registered rules (written before the OOS run; thresholds are the live code's)
All checks are as-of the close of the signal bar i.

| # | veto | exact rule (blocked when true) | source |
|---|---|---|---|
| V1 | premium > +0.3% | perp premium index close of bar i > +0.3%. Also reported as 4 buckets: ≤ −1%, −1..0%, 0..+0.3%, > +0.3% | SCRIPTS contradiction 2 |
| V1b | perp-led | 6h perp return − 6h spot return > 0.3 pp, and the premium widened > 10 bp over those 6h (needs spot) | `signals/tape.perp_vs_spot` (+5 points live, "fade candidate") |
| V2 | negative funding | last funding print < 0, or a 1h funding interval | `signals/core.funding` (2 vetoes) |
| V3 | spot-led rally | 6h perp return > 0, 6h spot return ≥ it, 6h spot taker imbalance > 0.10, and premium ≤ 0 (needs spot) | `signals/tape.perp_vs_spot`, `tape.spot_led_rally` |
| V4 | long flush | a bar in i−2..i with a 1h return ≤ −2 × ATR% and OI contracts (OI USD / close) down ≥ 3% in that hour | TAPE #9 proxy (no liquidation WS history) |
| V5 | taker-buy burst | TI of bar i has a z-score > 2 vs the prior 23 hourly TIs (population sd), and close > open | `signals/tape.taker_imbalance` |

**V4 event studies** run on all tradable coins, with a 6-bar cooldown per coin:
- **flush_short:** short the close of a long-flush bar. This tests continuation vs the "don't chase" bounce.
- **squeeze_short:** short the close of a squeeze bar (1h return ≥ +2 ATR% with OI contracts −3%). Does an OI-down squeeze reverse?
- Both use the random baseline's plan: stop 2 ATR above, half at 1R and the rest at 2R, breakeven 1R, 24h.
- Also reported: the raw 4h / 24h / 72h forward return (median, and % of events lower) against the same-hour placebo.

**Ledger:** 47 new trials, for a total of **N = 123** in the DSR.
- Per base: 3 premium buckets, plus blocked and allowed for each of the 6 rules. The > +0.3% bucket is V1's blocked trial.
- Plus the 2 event trials.

**Verdict per (veto, base), fixed now:**
- **needs more data:** blocked or allowed OOS n < 30.
- **INVERT-TO-SHADOW:** the blocked trial passes the adopt-to-shadow bar (OOS > 0, beats the same-hour placebo in both halves, stress ≥ 0) and does at least as well OOS as the allowed trial.
- **KEEP:** the blocked trades do worse OOS than the allowed trades in **both** avg R and the same-hour paired edge.
- **DROP:** anything else, i.e. the blocked trades do as well or better OOS on at least one of the two.

The overall call per veto rests on pump_fade and level_fade, the bases with n. pump_fade+discount is reported, but most of its cells will be below n 30. Event trials get the round-1 strategy verdicts. For V1b the "blocked" side is the perp-led set: KEEP there would mean perp-led fades do *worse*, which contradicts TAPE #7's bonus.

<!-- round-3 results below were added AFTER the single OOS run -->

### 3. OOS results: one run, 2026-09-29 (`--round3`, N = 123 trials, V[SR] = 0.0253, so SR\* ≈ 0.41 per trade)
The round 0–2 rows reproduce exactly (pump_fade n 2211, pump_fade+discount n 133, OOS +0.17). At N = 123, pump_fade+discount's DSR is **0.01**.

**How to read the table:**
- R is net of all costs.
- Edge is the paired edge vs the same-hour placebo (day-clustered t), shown as all, then IS/OOS.
- Δ is OOS blocked − allowed R, with a day-clustered t.
- Every DSR is ≤ 0.09.
- p+d is the pump_fade+discount base.

| veto | base | side | n (coins) | IS R | OOS R (n) | edge (t) IS/OOS | stress | Δ OOS (t) | verdict |
|---|---|---|---|---|---|---|---|---|---|
| V1 prem > +0.3% | pump_fade | blocked | 53 (35) | −0.13 | +0.05 (24) | −0.02 (−0.4) −0.14/+0.12 | −0.09 | +0.11 (0.5) | needs data |
| | | allowed | 2158 (507) | −0.09 | −0.07 (1090) | +0.09 (3.3) +0.12/+0.06 | −0.13 | | |
| | level_fade | blocked | 110 (63) | −0.11 | −0.26 (52) | −0.02 (−1.0) +0.09/−0.13 | −0.24 | −0.08 (−0.5) | **KEEP** |
| | | allowed | 9086 (527) | −0.16 | −0.18 (4662) | +0.04 (2.8) +0.05/+0.03 | −0.23 | | |
| V1b perp-led (bonus) | pump_fade | perp-led | 32 (26) | +0.14 | +0.24 (12) | +0.29 (2.7) +0.21/+0.42 | +0.15 | +0.31 (2.2) | needs data |
| | | other | 2179 (507) | −0.09 | −0.07 (1102) | +0.09 (2.6) +0.10/+0.08 | −0.14 | | |
| | level_fade | perp-led | 152 (86) | −0.12 | +0.01 (62) | +0.15 (2.0) +0.14/+0.18 | −0.11 | +0.20 (1.4) | DROP (the bonus stands) |
| | | other | 9050 (527) | −0.16 | −0.18 (4657) | +0.02 (1.6) +0.06/−0.02 | −0.23 | | |
| V2 neg funding / 1h | pump_fade | blocked | 282 (151) | −0.04 | +0.00 (145) | +0.14 (3.1) +0.20/+0.09 | −0.06 | +0.07 (0.9) | **DROP** |
| | | allowed | 1929 (489) | −0.10 | −0.07 (969) | +0.07 (2.4) +0.10/+0.04 | −0.14 | | |
| | p+d | blocked | 108 (64) | +0.11 | +0.16 (57) | +0.15 (2.7) +0.23/+0.08 | +0.11 | −0.03 (−0.1) | needs data (allowed n 12); blocked passes the shadow bar |
| | | allowed | 25 (22) | −0.06 | +0.19 (12) | +0.23 (1.5) −0.04/+0.51 | +0.04 | | |
| | level_fade | blocked | 984 (303) | −0.00 | −0.13 (449) | +0.12 (2.8) +0.18/+0.05 | −0.11 | +0.06 (0.7) | **DROP** |
| | | allowed | 8349 (526) | −0.17 | −0.19 (4340) | +0.02 (1.4) +0.03/+0.01 | −0.24 | | |
| V3 spot-led rally | pump_fade | blocked | 228 (162) | −0.23 | +0.07 (129) | +0.07 (0.4) −0.06/+0.17 | −0.12 | +0.16 (1.8) | **DROP** (sign flips between halves) |
| | | allowed | 1984 (491) | −0.07 | −0.08 (985) | +0.09 (3.1) +0.12/+0.06 | −0.13 | | |
| | level_fade | blocked | 1007 (326) | −0.25 | −0.20 (585) | −0.01 (0.7) −0.02/−0.00 | −0.29 | −0.02 (−0.2) | **KEEP** (marginal) |
| | | allowed | 8597 (526) | −0.15 | −0.18 (4379) | +0.03 (2.2) +0.05/+0.01 | −0.22 | | |
| V4 long flush ≤ 3h | pump_fade | blocked | 51 (46) | +0.09 | −0.12 (22) | +0.15 (1.8) +0.25/+0.02 | −0.01 | −0.06 (−0.5) | needs data |
| | | allowed | 2160 (505) | −0.09 | −0.06 (1092) | +0.11 (3.8) +0.12/+0.11 | −0.14 | | |
| | level_fade | blocked | 157 (133) | +0.29 | −0.20 (39) | +0.05 (−0.8) +0.08/−0.06 | +0.14 | −0.02 (−0.2) | **KEEP** (marginal) |
| | | allowed | 9030 (527) | −0.17 | −0.18 (4664) | +0.03 (1.9) +0.04/+0.02 | −0.23 | | |
| V5 taker-buy burst | pump_fade | blocked | 5 (4) | −1.16 | −0.28 (4) | – | −0.54 | – | needs data (inert) |
| | level_fade | blocked | 211 (167) | −0.26 | −0.18 (111) | −0.01 (−0.3) −0.08/+0.05 | −0.28 | +0.00 (0.0) | **DROP** (inert) |
| | | allowed | 9048 (527) | −0.16 | −0.18 (4645) | +0.04 (2.7) +0.06/+0.01 | −0.23 | | |

**Premium buckets** (OOS R (n) / stress / p90 MAE):
- **pump_fade:**
  - ≤ −1%: **+0.20 (33)**, stress +0.12, p90 MAE 9.9%, t_day 2.1, DSR 0.09. It passes the shadow bar, but these are the same 52 trades as pump_fade+discount's ≤ −1% cell.
  - −1..0%: −0.07 (870), p90 MAE 7.2%.
  - 0..+0.3%: −0.07 (188), p90 MAE 8.3%.
  - \> +0.3%: +0.05 (24), p90 MAE **14.8%**, the widest.
- **level_fade:** −0.08 (35), −0.19 (4020), −0.12 (836), **−0.26 (52)**.

**Funding actually paid** (R per trade, negative = the short paid; mean / p10 / share paying ≥ 0.1R):
- V2-blocked pump_fade: −0.012 / −0.031 / 3%.
- p+d blocked: −0.028 / −0.078 / 7%.
- level_fade blocked: −0.019 / −0.058 / 6%. The allowed books pay about 0.

At 12–24h holds the funding cost of shorting into negative funding is about 0.01–0.03R. The bad-funding days are real, but small against the edge.

**V4 event studies** (shorting the close of the event bar; random-baseline exits):

| event | n (coins) | IS R | OOS R (n) | edge (t) IS/OOS | stress | raw fwd short 24h, median % / % lower (IS; OOS) | 72h (IS; OOS) |
|---|---|---|---|---|---|---|---|
| flush_short (≤ −2 ATR, OI −3%) | 749 (377) | +0.22 | **−0.30** (320) | +0.05 (2.7) +0.10/−0.02 | −0.05 | +4.69/73; −0.95/42 | +1.44/61; −2.90/34 |
| squeeze_short (≥ +2 ATR, OI −3%) | 79 (67) | −0.26 | −0.37 (35) | +0.04 (−0.2) +0.23/−0.20 | −0.38 | −0.80/48; +0.06/51 | +1.12/50; +0.78/61 |

**The IS flush result is one day.** On 2026-08-22, a market-wide cascade produced 202 of the 429 IS flush events and +129R. Excluding that day, IS is −0.19R. The level_fade flush-blocked IS of +0.29 comes from the same day.

#### Verdicts (pre-registered rule; overall call on pump_fade + level_fade)
| veto | verdict | why |
|---|---|---|
| V1 premium > +0.3% | **KEEP (weak)** | level_fade blocked −0.26 vs −0.18 OOS, and edge −0.13 vs +0.03. pump_fade IS −0.13 vs −0.09, with the widest p90 MAE (14.8%). No cell is significant (t ≤ 0.5), and nothing is near SCRIPTS' −6.2% / 131% MAE, which came from 72h holds at +35% crossings. |
| V1b perp-led = fade (+5 pts) | **bonus stands** (TAPE #7 not contradicted) | Perp-led fades did *better* on both bases: +0.24 vs −0.07 (n 12, t 2.2) and +0.01 vs −0.18 (t 1.4). The *level* of the premium and its *widening* are different signals, and SCRIPTS' contradiction 2 conflated them. |
| V2 negative funding / 1h interval | **DROP** | Blocked beat allowed OOS on both bases (+0.07, +0.06R) and in edge in both halves. Funding costs 0.01–0.03R. The veto blocks **81% (108/133)** of the one shadow candidate, pump_fade+discount, whose blocked part passes the shadow bar on its own (+0.16 OOS, stress +0.11). |
| V3 spot-led rally | **split: no stable effect** | pump_fade: DROP (+0.07 vs −0.08 OOS, but −0.23 vs −0.07 IS, so the sign flips). level_fade: KEEP by 0.02R (t −0.2). The forensics "contradiction" is not confirmed, and neither is the veto. |
| V4 long-flush "don't chase" | **KEEP** | Shorting right after a flush lost OOS (−0.30R). Outside one market-wide cascade day it lost IS too. The raw 72h OOS shows a bounce: 34% of events lower. SCRIPTS' "long flushes kept falling" (−20%/12h events) does not transfer to the 1h proxy. |
| V4b OI-down squeeze → reversal | **reject as a short** | −0.26 / −0.37R. The 72h raw reversal is weak (61% lower OOS, n 35). |
| V5 taker-buy burst | **DROP (inert)** | It fires on 0.2% of pump_fade and 2.3% of level_fade signals. Blocked = allowed (−0.18 vs −0.18). |

#### What round 3 taught us
1. **No veto changes the sign of a book.** Every Δ is ≤ 0.2R, and only one has |t| ≥ 2: perp-led on pump_fade, n 12. The vetoes matter less than the base situation. The tide (round 1 F1) is still the only veto with a consistent effect.
2. **The negative-funding veto is the one that costs something.** It removes most of the discount-squeeze candidate, and its blocked trades beat its allowed ones on both large bases.
3. **Recommended live changes (the human decides; nothing was changed):**
   - (a) Turn the `funding` negative-rate / 1h-interval vetoes into **points or a sizing note plus a funding budget**, not a hard veto. At minimum, exempt the discount_squeeze state.
   - (b) Keep `liquidation_cascade`.
   - (c) Keep the perp-led +5 points, and add a *soft* flag for a premium level > +0.3%.
   - (d) `taker_imbalance` and `spot_led_rally` may stay, but they are not evidence-based. Record them as unproven hypotheses with an effect of about 0.
4. The **premium ≤ −1% at entry** cell of pump_fade (+0.20 OOS, stress +0.12, DSR 0.09) is the discount candidate again, not a new one. It carries the same contamination warning as F4 and needs the forward shadow window.


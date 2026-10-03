#!/usr/bin/env bash
# Backfill the learnings audit fields for the rulebook (2026-10-03): kind, n/days/sample, rule, valid_until and
# superseded_by on learnings #1-#83. Values come only from each learning's own statement and evidence: n is set
# only where the text states the count of independent observations (conservative for rows recorded before
# 2026-10-01); everything else is left for the next research review, so it stays out of the rulebook.
#
# Idempotent: every command sets a value; nothing appends (no --evidence, no --provenance). A rerun writes the
# same values and only bumps updated_at (which restarts the default review_by clock).
#
#   local:  SB=.venv/bin/shortbot bash deploy/backfill/learnings-2026-10-03.sh
#   server: SB="docker compose run --rm guardian shortbot" bash deploy/backfill/learnings-2026-10-03.sh
set -euo pipefail
SB="${SB:-.venv/bin/shortbot}"
exec </dev/null   # no $SB call (e.g. docker compose run) may read the script's stdin

die() { echo "backfill aborted, nothing written: $*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight (read-only)
# 1. the new learnings code is deployed (rule / superseded_by / valid_until columns).
help_text=$($SB update-learning --help 2>&1) || die "'$SB update-learning --help' failed: $help_text"
grep -q -- --rule <<<"$help_text" || die "update-learning has no --rule: deploy the new learnings code first"

# 2. every id this script touches, plus all of #54-#83 (written on the Mac before the desk moved to AWS), exists
#    and is the same learning: "id|a phrase from its statement" (statements never change).
CHECKS=(
  "1|Obvious levels get swept before they turn"   "2|A stop swap is cancel-then-place (Binance"
  "3|Trail updates must compare the"              "4|Measured level sweeps (60 most liquid perps,"
  "5|Sessions shape stop runs. The ONDO"          "6|Session behaviour at levels (7 days, 60"
  "7|A manual partial close must count toward"    "8|The open-interest floor (M) is a hypothesis,"
  "9|Exposure should follow EDGE (odds above"     "10|Laggard catch-up, baskets and adaptive"
  "11|Invalidations for level fades must mean"    "12|Week event study (460 perps, 09-19..09-29,"
  "13|Liquidity floors (research): thin coins"    "14|Managing a growing pattern library ="
  "15|Listing lifecycle (267 USDT perps listed"   "16|Move forensics, 49 episodes / 60d"
  "17|60-day backtest (07-27..09-29, 649 perps"   "18|Literature sanity check (14 rules, 29"
  "19|Failure analysis round 1 (in-sample"        "20|Failure round 2 (23 more trials, ledger 76;"
  "21|Analog episodes (kNN on 30 z-scored as-of"  "22|Failure round 3 - are the live vetoes right"
  "23|CORRECTION from the backtest code review"   "24|Round 4 corrections (volatility-matched"
  "25|Hedging alt shorts with BTC (research,"     "26|BTC hedge backtest (12 pre-registered"
  "27|Limit entries must pass the same signal"    "28|Instrument/universe research (2026-09-30):"
  "29|Why no trades for 2 days: mostly the week"  "30|Multiple concurrent positions: hold several"
  "31|book-depth floor (task"                     "32|Realistic benchmark for a directional crypto"
  "33|Funding/carry: use it as a SIGNAL, not a"   "34|Academic cross-sectional crypto premia live"
  "35|Tail risk is venue mechanics, not position" "36|within months across venues, which"
  "37|How consistent winners are structured, and" "38|Funding, corrected rule (replaces"
  "39|The first long sleeve has no edge yet."     "40|Positioning signals (open interest,"
  "41|Cross-sectional factor books on liquid"     "42|Take-profit rungs (P1a), evidence-based"
  "43|Per-setup drawdown ladder (P2),"            "44|Token unlock effect sizes (verified source)"
  "45|Remaining library cards tested on our data" "46|Long edges exist only at daily/weekly"
  "47|Hourly long triggers fail: buying a 3d/7d"  "48|buy coin-specific crashes, especially in"
  "51|Through the LIVE pipeline (backtest replay" "52|Chaining timeframes improves pump shorts"
  "53|Hypotheses for the forward record: (1) S5"  "54|Per-coin scale and character traits carry"
  "55|of multi-timeframe rules does not predict"  "56|At 1m-5m, tape regime (quiet vs active) is"
  "57|AI forecast on one coin (QNT, cutoff Thu"   "58|Desk demo results so far come from scan"
  "59|QNT post-pump down-leg, two-phase bounce:"  "60|Retail-flip flush tell (QNT, post-pump):"
  "61|On a high-vol post-pump coin, range-edge"   "62|Limit placement: put fade/bounce limits AT"
  "63|Kill conditions are free text the guardian" "64|Stops can sit inside the coin"
  "65|Registry status is advisory only: open"     "66|Entry prices and time stops are left to the"
  "67|Exchange-side closes get no exit reason or" "68|own probability is almost never logged"
  "69|5m positioning data (OI, top/global L/S,"   "70|archived: the live store holds about 8h"
  "71|persisted with timestamps, so news"         "72|The unlock calendar is empty (0 rows), so"
  "73|Registry status never changes automatically" "74|There is no per-coin memory or watchlist:"
  "75|Signal half-lives are priors that have never" "76|The two replay tells (two-phase bounce"
  "77|One network outage floods the desk with"    "78|The MCP server keeps running stale code"
  "79|Single-coin replay, Friday morning:"        "80|Final result of the blind single-coin 15-min"
  "81|Exits should be hybrid, not strictly"       "82|The journal does not scale: the session"
  "83|How the desk should learn (research-backed"
)
for check in "${CHECKS[@]}"; do
  id=${check%%|*} phrase=${check#*|}
  out=$($SB learnings --id "$id" 2>&1) || die "'$SB learnings --id $id' failed: $out"
  grep -q "\"id\": $id," <<<"$out" \
    || die "learning #$id does not exist here. #54-#83 were written on the Mac before the move to AWS: copy them over first"
  grep -qF -- "$phrase" <<<"$out" \
    || die "learning #$id is a different learning here (its statement lacks: $phrase). The ids no longer match the Mac's"
done
echo "preflight ok: ${#CHECKS[@]} learnings present and matching"

# ---------------------------------------------------------------- backfill
{
# --- engineering: code fixes backed by a named test (never in the rulebook)
# #2 stop swap cancel-then-place (test_move_stop_drift_*)
$SB update-learning 2 --kind engineering
# #3 same-tick trail noop (test_move_stop_same_tick_is_a_noop_without_swapping)
$SB update-learning 3 --kind engineering
# #7 manual partials count toward cumulative targets (test_manual_partial_*)
$SB update-learning 7 --kind engineering
# #27 limit entries pass the signal vetoes (test_limit_entry_refuses_a_vetoed_read_unless_overridden)
$SB update-learning 27 --kind engineering

# --- process hypotheses: how the desk decides, builds or manages risk
# #8 OI floor decided by the by_open_interest buckets
$SB update-learning 8 --kind process
# #9 exposure ladder follows edge, shadow mode
$SB update-learning 9 --kind process
# #10 laggard / basket / re-level / tuning research design
$SB update-learning 10 --kind process
# #13 liquidity tiers to test
$SB update-learning 13 --kind process
# #14 pattern registry, episodes, trial log
$SB update-learning 14 --kind process
# #28 instrument and universe recommendation
$SB update-learning 28 --kind process
# #31 log book depth and test before any depth veto
$SB update-learning 31 --kind process
# #35 venue tail-risk rules (margin asset, ADL, TP ladders)
$SB update-learning 35 --kind process
# #37 how consistent winners are structured
$SB update-learning 37 --kind process

# --- statistical hypotheses (outside the rulebook by status)
# #1 ONDO trade #5 sweep, one trade on one coin-day: n=1, expires
$SB update-learning 1 --kind statistical --n 1 --days 1 --sample 'trade:5 ONDO 2026-09-28' --valid-until 2026-11-02
# #5 session stop runs: #6 says it supersedes #5 (7-day rerun of the same sweep study)
$SB update-learning 5 --kind statistical --superseded-by 6
# #12 week event study: five hypotheses on different subsamples, no single n
$SB update-learning 12 --kind statistical
# #16 move forensics: several scripts on different samples (49 episodes, 303 crossings), no single n
$SB update-learning 16 --kind statistical
# #25 BTC hedge research (literature, no own sample)
$SB update-learning 25 --kind statistical
# #29 why no trades: one passing week (09-28..10-01), 414 scan episodes with a full 24h outcome over 3 days
$SB update-learning 29 --kind statistical --n 414 --days 3 --sample 'scratchpad fwd2.py, scan episodes 2026-09-28..10-01' --valid-until 2026-11-02
# #48 coin-specific crash continuation: counts given only as ~420 / ~165
$SB update-learning 48 --kind statistical
# #53 S5 / L2 chain hypotheses: smaller sample S5 n 148, 93 days
$SB update-learning 53 --kind statistical --n 148 --days 93 --sample 'data/research/2026-10-03-e5aed86-dirty/chain_study'

# --- adopted
# #11 invalidation needs acceptance: one trade (ZRO #8, 2026-09-29); adopted, so it stays in the rulebook
$SB update-learning 11 --kind statistical --n 1 --days 1 --sample 'trade:8 ZRO 2026-09-29' \
  --rule 'Level-fade invalidation needs acceptance: 2 consecutive 1h closes beyond the level, or a close past its fake-out p90'

# --- supported process: settled by status (no sample gate)
# #30 concurrent positions inside one risk budget
$SB update-learning 30 --kind process \
  --rule 'Hold several shorts only inside one fixed risk budget: per-trade risk <= cluster cap / target N, all alt shorts one cluster, slots 4-5 only for event setups'
# #32 realistic benchmark for a directional desk
$SB update-learning 32 --kind process \
  --rule 'Judge setups against real directional desks: about 40% winners, 8-18% monthly drawdowns, net Sharpe 0.5-1.0; a directional Sharpe above 3 is not credible'
# #43 per-setup drawdown ladder
$SB update-learning 43 --kind process \
  --rule 'Per-setup drawdown ladder: halve at about 12 sigma (9R), stop to shadow at about 17 sigma (12.5R) of its per-trade R from the high-water mark'

# --- supported statistical with a stated sample (n >= 30: settled)
# #15 listing lifecycle: 267 listings 2025-03..2026-09; n = the HODLer subgroup (53) the shortable claim rests on
$SB update-learning 15 --kind statistical --n 53 --sample 'docs/LIFECYCLE_STUDY.md, data/lifecycle.db (2026-09-29)' \
  --rule 'Listing fade: short HODLer-airdrop or spot-listed perps near the day-7 close, wide stop, long hold; skip Alpha and perp-only listings (half pump again)'
# #36 2025 listings decay: Odaily study, Binance cohort n=92 (third-party practitioner study)
$SB update-learning 36 --kind statistical --n 92 --sample 'Odaily study: 2025 Binance listings, day-1 close to 2026-03-11' \
  --rule 'Rank listing-fade shorts by supply: zero-cost-basis recipients (HODLer, Launchpool, Alpha graduate), float < 25% with FDV > 1B; MM deals are supply events'
# #38 funding corrected rule: 676 perps, daily 2020-07..2026-08 (replaces #33)
$SB update-learning 38 --kind statistical --n 676 --sample 'docs/REGIME_STUDIES.md tasks #12/#13, daily 2020-07..2026-08' \
  --rule 'Cut broad or beta alt short size when 7d BTC/ETH carry is in its top yearly quintile (trend-on); high funding on a coin is never a short trigger'
# #41 factor books: CTREND 291 weeks (blend 2,355 days); n = the smaller
$SB update-learning 41 --kind statistical --n 291 --sample 'docs/REGIME_STUDIES.md s3, registry trials xs_daily' \
  --rule 'Factor books (CTREND, carry/momentum/breakout blend) earn nothing on the short leg of liquid perps: never use factor ranks to pick shorts'
# #42 take-profit rungs: 34,800 hourly / 5,281 daily trades; n = the smaller
$SB update-learning 42 --kind statistical --n 5281 --sample 'backtest/rungs.py, registry trials rungs_60d / rungs_daily' \
  --rule 'Never carve take-profit rungs out of automatic targets; rest 6/12/20% reduce-only rungs only on the part with no automatic target, up to 70% of it'
# #44 unlock effect sizes: 16,000 unlock events on 40 tokens (Keyrock, third-party); n = tokens
$SB update-learning 44 --kind statistical --n 40 --sample 'Keyrock 16,000-unlock study, fetched 2026-10-02' \
  --rule 'Unlock short: enter 2-4 weeks before large team or cliff unlocks, size up for 5-10% of supply, exit around the unlock; skip ecosystem unlocks'
# #46 daily long edges: 1,658 RS name-weeks / 2,007 breakout trades; n = the smaller
$SB update-learning 46 --kind statistical --n 1658 --sample 'data/research/2026-10-02-72f19f0/long_studies' \
  --rule 'Long playbook: take every daily RS-leader or 20d-breakout signal at equal size, exit by time plus a catastrophic stop; no targets, breakeven or trailing'
# #47 hourly long triggers fail: 748-1,022 and 327 trades; n = the smallest
$SB update-learning 47 --kind statistical --n 327 --sample 'data/research/2026-10-02-72f19f0/bt60_long2' \
  --rule 'Longs act on daily closes only: hourly 3x-volume breakout and RS-pullback long entries do worse than a random coin bought the same hour'
# #52 chained timeframes: S0 n 2423, S4 n 1151, 93 days; n = the chain end
$SB update-learning 52 --kind statistical --n 1151 --days 93 --sample 'data/research/2026-10-03-e5aed86-dirty/chain_study' \
  --rule 'Pump shorts: chained 15m/1h lower highs and 5m taker selling help only marginally; tail squeezes decide the mean, so manage squeezes over entry timing'
# #51 pump_fade through the live pipeline (n=725, 56d already set)
$SB update-learning 51 \
  --rule 'Through the live pipeline (Aug-Sep 2026 replay) pump_fade loses about 0.07R per episode after costs; entering 5-15 min late costs 0.02-0.03R more'
# #54 per-coin traits (n=520, 68d already set)
$SB update-learning 54 \
  --rule 'Estimate scale (vol, wick share, swing size) per coin but learn direction rules pooled across coins: per-coin direction does not persist'
# #55 single-coin mold (n=44, 29d already set)
$SB update-learning 55 \
  --rule 'Never mine rules on one coin: single-coin 30-day rules at 80% in-sample fall to a coin flip on the next 10 days, no better than shifted noise'
# #56 1m-5m tape regime (n=44, 61d already set)
$SB update-learning 56 \
  --rule 'At 1m-5m only the tape state (quiet vs active) is predictable, alike across coins; direction and trend continuation are not, so never fit per coin'

# --- supported statistical without a usable n (stays out of the rulebook until a review states one)
# #4 sweeps, 3 days: event counts the same study calls correlated (#6), not independent
$SB update-learning 4 --kind statistical \
  --rule 'Level sweeps: 24h highs are the best short levels; round numbers fail no more often but run further, so stop past the p90 fake-out and clear of rounds'
# #6 sweeps by session, 7 days: its own evidence calls the events correlated, hypothesis-grade
$SB update-learning 6 --kind statistical \
  --rule 'Sweeps by session (1 week): Asia breakouts fail most but run furthest first, London breakouts hold (weakest fade), weekend round numbers run furthest'
# #17 60-day backtest: only the coin universe is stated, not the trades behind the claim
$SB update-learning 17 --kind statistical \
  --rule '60-day backtest: no short-only strategy survived costs, OOS and multiple testing; vetoes that cut losses: BTC +2% in 24h, one-way trends, near the 24h high'
# #18 literature check: no own sample
$SB update-learning 18 --kind statistical \
  --rule 'Literature agrees with our null: no evidence that short-only altcoin books net a profit; MAX shorts, L/S-ratio and session rules lack support'
# #19 failure round 1: several claims on different samples
$SB update-learning 19 --kind statistical \
  --rule 'Tide veto (no short when BTC 24h >= +2%, BTC 4h >= +1% or sector 4h >= +1%) trims losses but creates no edge; filters picked in-sample lose most gain OOS'
# #20 failure round 2: no counts stated
$SB update-learning 20 --kind statistical \
  --rule 'In breakdowns never rest bounce-sell limits (they fill on the failures); fades keep auto breakeven at +1R but turn auto trailing off; no re-entry after a stop'
# #21 analog episodes: no counts stated
$SB update-learning 21 --kind statistical \
  --rule 'Analog (kNN) episodes trim losers but create no edge: a family gets a live analog panel only while its OOS rho > 0 with t >= 2 and a positive quintile spread'
# #22 failure round 3 vetoes: no counts stated
$SB update-learning 22 --kind statistical \
  --rule 'No veto flips a book sign: soften the negative-funding veto (it blocked better trades), keep perp-led points and long-flush no-chase; only the tide veto holds'
# #23 backtest correction: the placebo claim has no stated count
$SB update-learning 23 --kind statistical \
  --rule 'The same-hour placebo was not volatility-matched, so most of the triggers-beat-random edge was cost; only forward data can judge pump_fade plus discount'
# #24 round 4: 516 coins / 60 days is the universe, not the trades behind the claim
$SB update-learning 24 --kind statistical \
  --rule 'Against a volatility-matched placebo only pump_fade plus discount keeps a relative edge (shadow only); in the rising Aug-Sep market every short book lost'
# #26 hedge backtest: the backtest trade count is not stated (plus 9 live trades)
$SB update-learning 26 --kind statistical \
  --rule 'Do not hedge single alt shorts with BTC or baskets: it removes almost no variance, lowers R on the only edge and deepens squeezes that come as BTC falls'
# #34 academic factor premia: literature samples only
$SB update-learning 34 --kind statistical \
  --rule 'Academic crypto factor premia sit in the long leg: use factor strength (CTREND, large-cap momentum, BTC/ETH strength) only as what not to short'
# #40 positioning signals: n given for one variant only (49)
$SB update-learning 40 --kind statistical \
  --rule 'OI, L/S, taker flow and crowded funding add little at 1h: skip crowded-unwind and retail-contrarian shorts; long-trap and squeeze-fuel are shadow-only'
# #45 library cards: five findings on samples from n=3 to 1,046 prints
$SB update-learning 45 --kind statistical \
  --rule 'Do not fade a high-funding coin into or right after an extreme funding print: the payer side keeps winning for about 2h; hour-of-day effects are too small'
# #39 first long sleeve: its evidence says superseded for the playbook by #46 and #47 (#47 restates its result)
$SB update-learning 39 --kind statistical --superseded-by 47 \
  --rule 'The first hourly long sleeve (flush reclaim, oversold bounce, capitulation, discount) has no edge beyond the up-drift; only the discount long is shadow-logged'
# #80 final QNT replay: one coin over two days, n=18 (anecdote); expires
$SB update-learning 80 --valid-until 2026-11-02 \
  --rule 'Blind QNT replay over 2 days: direction was a coin flip and coin tells lasted a day; only the liquidation-wick sequence read after it printed repeated'

# --- one-coin / one-day replay anecdotes: expire unless extended
$SB update-learning 57 --valid-until 2026-11-02   # QNT blind forecast, one coin-week
$SB update-learning 59 --valid-until 2026-11-02   # QNT two-phase bounce, 2026-10-01
$SB update-learning 60 --valid-until 2026-11-02   # QNT retail-flip flush tell, 2026-10-01
$SB update-learning 61 --valid-until 2026-11-02   # QNT range-edge overshoot, 2026-10-01
$SB update-learning 62 --valid-until 2026-11-02   # QNT limit placement, 2026-10-01
$SB update-learning 79 --valid-until 2026-11-02   # QNT Friday morning, 2026-10-02

# --- explicit supersedes
# #33 (rejected) -> #38: "Funding, corrected rule (replaces #33)"
$SB update-learning 33 --superseded-by 38
# #63 -> #81: "Supersedes the auto-exit design in #63"
$SB update-learning 63 --superseded-by 81
} >/dev/null

# --- #82/#83 (the design this implements): one evidence line, appended once (guarded, so a rerun adds nothing)
for lid in 82 83; do
  $SB learnings --id "$lid" | grep -q 'deployed ff09474' ||
    $SB update-learning "$lid" --evidence "Implemented 2026-10-03: learnings rulebook, FTS search, uses_learnings outcome scores, supersede/expiry (4dc8ec7) and coin memory (1a1dd35); deployed ff09474 15:34 UTC. Open question: whether the rulebook and scores improve decisions; judge after 30+ trades that cite learnings." >/dev/null
done
echo "backfill done"

# ---------------------------------------------------------------- result
$SB brief | sed -n '/### rulebook/,/### learnings to re-check/p'

# The 3-level ladder: exposure that follows the trade's current odds (research 2026-09-28)

**Verdict: sound, with conditions.**
1. The "probability" must be EDGE over the no-edge baseline, not raw p.
2. It must pass calibration before it drives real size.
3. Changes need hysteresis and a cost gate.
4. The 1R budget never grows.

Labels: [S] peer-reviewed, [M] math, [W] preprint, [P] practitioner.

## Why
- **Log utility looks only forward:** the right exposure at each moment is Kelly on the forward outcome distribution. Entry price and P&L so far are sunk. With learning, you plug in the filtered edge (Gabih-Kondakji-Wunderlich, arXiv 2301.06847). [S/W]
- **Binary Kelly from the current price** (stop a away, target b away): f = p/a − q/b.
  - At the martingale baseline p₀ = a/(a+b), f = 0.
  - With edge δ = p − p₀, the equity risked is δ(1 + a/b).
  - So **exposure is a function of edge, not of p.** [M]
  - With more than two outcomes (the ladder), f ≈ E[R]/E[R²], then apply a fractional haircut.
- **Frictions:** trade only part of the way toward an "aim" position (Gârleanu-Pedersen). That is the basis for hysteresis. [S]
- **Scaling in:** it loses to all-in when the edge is largest at the trigger (Vanguard). Tranches only pay when an add depends on NEW information. [P]
- **Pyramiding:** Turtle units match our add rule. [P]
- **Averaging down:** never near the stop (Leung-Li: the optimal entry region is bounded away from the stop). [S]
- **Taking winners early by habit is a bias** (Odean). [S] No clean study shows "reduce on falling odds" beats "hold to stop" in general.
- **The big pitfall:** if p comes from price alone, any exposure path is a martingale transform, with expected P&L 0 before costs (optional stopping).
  - Cutting as price rises against a short is replicating convexity. On fades it sells the fake-out wick, the best short price (Kaminski-Lo: stops cost value under mean reversion).
  - **Only inputs that move edge at a FIXED price distance add value:** squeeze state, tape flips, BTC/breadth regime flips, time decay, and MAE vs winners' MAE.
- **Costs:** a round trip ≈ 0.2% (fees + slippage), so each resize costs ≈ 0.2%/stop% in R. That's 0.1R on a 2% stop.

## The ladder (for a SHORT)
- **Levels are fixed at entry.** Only the probabilities are recomputed, except the stop, which can only tighten.
  - **Stop:** structure plus the measured fake-out (`sweep_proof`), beyond the 85th percentile of winners' MAE once known.
  - **T1/T2/T3 targets:**
    1. our MFE percentiles (30/50/70) once measured
    2. otherwise structure (VWAP, pre-pump base, retrace)
    3. otherwise 1/2/3 × σ√T.
  - **Entry layers:** only inside a pre-planned entry zone, with total risk ≤ 1R, and never in the last 50% of the entry-to-stop distance. Adds after entry: only at ≥ +1R (existing rule).
- **Per-level odds:** P(Tᵢ before the stop, within the time left), with nested P(T1) ≥ P(T2) ≥ P(T3).
  - p₀ is the no-edge baseline (Cox-Miller, `probability/baseline.py`).
  - p_model adds edge as drift in a first-passage formula. The drift comes from the STATE SCORE, never from the coin's own recent returns (that would be momentum extrapolation).
- **Per-level EV:** EVᵢ = Pᵢ·bᵢ − P(stop)·a − c. At the baseline every EVᵢ ≈ −c.
  - At each target Tᵢ₋₁, keep the tranche aimed at Tᵢ only if that continuation bet has EV > 2c.

## Exposure rule (thresholds are assumptions until calibrated)
- **State score s ∈ [−2, +2],** a rule-based sum until the probability model passes its gate (≥ 200 labels, reliable):

  | Input | Contribution |
  |---|---|
  | Squeeze: OI and price rising together | −1 |
  | Squeeze: funding turns negative | −1 |
  | Squeeze: taker-buy burst with price up | −1 |
  | Tape: failed sweep, post-pump decay or hype fade | +1 |
  | Tape: accepted breakout, pump in progress or attention rising | −1 |
  | Market: breadth thrust +15pp since entry | −1 |
  | Market: BTC/ETH impulse pause | −1 |
  | Market: deep RISK-OFF (breadth < 20%) | +0.5 |
  | Time decay: past half the time stop with MFE < 0.5R | −0.5 |

- **Target exposure:** x* = budget × clip(0.5 + s/4, 0, 1). So s = +2 is full budget, 0 is half, −2 is flat.
- **Initial tranche:** 60% of budget on a strong trigger, 40% otherwise.
- **Hysteresis:** act only when |x* − x| ≥ 25% of budget for 2 consecutive 15-minute checks, at least 30 minutes between changes, at most 2 adds and 2 reductions.
- **Cost gate:** 0.2% / stop% ≤ 0.05R.
- **Risk:** reductions never move the stop. Adds tighten it so the worst case stays ≤ 1R.

## Shadow mode (now)
- **The guardian logs the ladder every 15 minutes for each open position** into `ladder_shadow`: price, per-level p₀ / p_model / EV, s and its parts, x*, actual exposure, and the would-be action.
- **Nothing trades from it.** Later it is scored from bars, with costs, against:
  1. the rule
  2. hold-to-plan
  3. a price-only placebo.
- **Adopt only if all hold:** the rule beats both (paired difference in R per trade, ~200 trades for +0.1R at 80% power), reductions predict stop-outs (≥ 50 events), per-level Brier/reliability pass, and drawdown does not get worse.

## Extensions (research 2026-09-28, part 2)

**A. Laggard catch-up:** SHADOW STUDY ONLY.
- Equities show slow lead-lag: big leads small within an industry at weekly horizons (Lo-MacKinlay 1990, Hou 2007). [S]
- Crypto 1-12h catch-up among alts is untested net of costs. Minute-level reversal is real but ~1.3 bp gross vs ~5 bp costs (arXiv 2608.21888). [W]
- The sign is unknown (catch-up or keeps lagging), so test both. Hurdle: +40 bp net of our ~20 bp costs, above a matched control.
- **Design:**
  - Sector members with 7-day residual correlation to the leader ≥ 0.5.
  - Leader = the highest-OI member.
  - Event = the leader shows acceptance below a level (2 × 15m closes).
  - Laggard = its residual move < ½β × the leader's move.
  - Hypothetical short at t₀ + 1 bar. Target β × the leader's move; invalidate if the leader reclaims its level or the laggard makes a new high; 12h time stop.
  - Log residual returns at 1/2/4/8/12h, MFE/MAE, time to target, and a random same-sector control.
  - Go only with ≥ 100 events on ≥ 30 days and t ≥ 3 (clustered by day).

**A2. Pairs / stat-arb:** not usable short-only. Use the "rich" residual as a ranking feature only.

**A3. Baskets:** yes, for one thesis. The gain is tail control, not variance: with ρ ≈ 0.7 and n = 4, σ drops only ~12%, and correlation rises in squeezes. What you do get is that a single-coin +40% squeeze costs only 1/n of R.
- **Sizing:** assume ρ = 1 at the stops, so Σ qᵢ·dᵢ = 1R, with weights ∝ 1/ATR.
- The basket counts as one position. Each leg must pass the OI floor, since slippage on the thinnest leg is the real cost.

**B. Adaptive levels:** yes, but only triggered by EVENTS, never by P&L.
- **Allowed triggers:** a new confirmed 1h swing, acceptance through a level or round number (2 closes), or a new sector/BTC regime state. At most one re-level per closed 1h bar.
- **Rules:**
  - The stop only tightens, to structure + k·ATR.
  - Targets may move either way by the same pre-registered function.
  - Nothing moves in the last 20% of the time stop.
- **Ghost trade:** every change keeps the original plan running as a shadow "ghost" trade; the metric is paired ΔR (adaptive − fixed).
- **It is rationalising when** the trigger is P&L or time in pain, when changes are asymmetric, or when no counterfactual is kept. Pre-committed levels reduce the disposition effect (Fischbacher et al. 2017).

**C. Learning each value:**
- Paired shadow variants, all scored on the same events (scan candidates too), plus empirical-Bayes shrinkage and a trial ledger. No bandits until real money is split among survivors.
- Tune one parameter family at a time, with at most 3 values each. The families: hysteresis/confirmations/gap, score weights, λ, level method, liquidity floors.
- **Sample size:** 0.1R at 80% power needs ~200 paired events (unpaired would need ~1,100).
- **Adopt a variant only if all hold:**
  - the shrunk Δ is at least the cost difference;
  - the deflated Sharpe (PSR) beats baseline at > 0.95, counting every variant ever tried;
  - it holds in the last walk-forward fold;
  - the change is made at a scheduled review, via `set_risk_params` with a tag.

## Live experiment: ladder_live_v1 (2026-09-29, demo only)
The human asked to act on the ladder instead of only shadowing it. It runs as a tagged experiment that is judged against the ghost of each trade's original plan.

**Opt in:**
- **A split entry** opts in automatically: `propose` with `tranches: [{limit_price, share}, {limit_price, share}]` (see `tranches.py`).
  - Two resting legs share one stop and one 1R budget. The first fill opens the trade and the second joins it as an add.
  - A leg still pending is cancelled when the trade closes. The normal TTL, `cancel_below` and fast-approach rules still apply.
  - The plan pins `full_size` (both legs) and `r1_usdt` (both legs at the stop), so exposure x = contracts / full_size, and a filled first leg reads x = 0.5.
- **An existing trade:** `shortbot ladder SYM --enable` pins 1R and full size first. This is a deliberate, logged choice.

**Act:** `shortbot ladder SYM --apply` (preview with `--dry-run`). The analyst runs it at reviews; the guardian never does, and autopilot stays off. It does three things:
- **Cut:** when the base variant's hysteresis says reduce, a partial close takes exposure down to x_virtual × full_size. Cuts under 10% are skipped.
- **Add:** when it says add, it calls `add_to_winner`, which only adds to winners, tightens the stop first, and keeps the loss at the stop ≤ the pinned 1R. "Double" is therefore only possible once the stop has tightened enough. If no stop at least `MIN_STOP_DISTANCE_PCT` from price fits, the add halves, and is skipped after two halvings.
- **Stop:** the adaptive variant's event re-level (a confirmed lower 1h swing high + 0.5 ATR), tighten-only.

**Judge:** `shortbot study ladder` → `ladder_live_v1.mean_delta_R_live_vs_ghost`. That is the paired ΔR of the real managed exposure against the ghost, where the ghost holds the initial exposure to the real exit.
- **Prior at launch:** on the first 4 trades every shadow variant trailed hold-to-plan by −0.2 to −0.37R, and base ≈ placebo.
- So the experiment starts with evidence against it. Keep it only if live beats the ghost over ~200 paired trades after costs (section C).

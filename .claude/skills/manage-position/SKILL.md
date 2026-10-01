---
name: manage-position
description: Manage an open short according to its own trade plan - re-check thesis, invalidation, kill conditions, targets, time stop, trailing; decide hold / tighten / partial / exit / update plan.
when_to_use: Reviewing open positions, a guardian alert (squeeze_risk, stop_near, time stop, review due, target reached, funding_negative, news_on_position, close_incomplete), or the user asks "how is my QNT short".
---

# Manage a position (per its plan)

Each trade carries a **plan** (from its setup's defaults plus the entry decision). Manage *that* trade, not a generic one.

## Inputs
- `positions`: plan, entry, exchange stop, MAE/MFE, targets_hit, age.
- `coin_snapshot <coin>`: structure, funding trend, OI, taker flow, news now.
- `journal`: the entry thesis (and the learnings for this setup).
- Deeper, ad-hoc questions: `shortbot.lab` via Bash (see `research-review`).

## Start zero-based: `.venv/bin/shortbot reassess <coin>`
Re-plan from the CURRENT price, as if entering now; the entry price is irrelevant (disposition effect, Odean 1998).
`reassess` returns R now, risk to stop in R and in ATR, the chandelier stop, reward:risk to the next target, the
ideal size for today's risk budget, and ranked actions. Rules: docs/POSITION_MANAGEMENT.md §b.
- **Exit** if you wouldn't open it now. Don't wait for the stop.
- **Reduce** (`close --fraction --expect-contracts <size you saw>`) if the size is above today's ideal. A partial close without the size you saw is refused for 5 min after any size change (the guardian may have just taken a target).
- **Tighten / trail** (`move-stop`), using the chandelier stop or structure.
- **Widen with size cut** (`widen-stop`): only once, beyond a sweep or structural level. The app cuts size so the loss stays ≤ 1R.
- **Add** (`add`): only in profit. The app tightens the stop first so the total worst case stays ≤ 1R. Never average up.
- **Time:** shorten on negative funding or when the trade isn't working by half the time stop; extend with `update_plan` when it's working.

## Decision checklist (in order)
1. **Protection**: is `UNPROTECTED` false and the stop where the plan says? If not, fix it first (`move_stop`, or ask the human to close).
   - `close_incomplete` (urgent): a full close left a remainder, because the close filled short or a resting entry filled mid-close. The alert names the stop that protects it now (kept, pre-armed, or just placed). Check `positions` and the stop on Binance before acting; don't re-close blindly. If the close filled short, the trade stays open: close it again if the exit reason still holds. If an entry filled mid-close, the fill poll adopts it as its own trade (urgent `action_fill`): manage it per its plan. If `NAKED_POSITION` comes with it, no stop could be placed: set one or close now. The guardian's 60 s pass is only the backstop.
2. **Invalidation / kill conditions**: has anything in `plan.invalidation` or `plan.kill_conditions` happened? If so, propose an exit now.
3. **Squeeze signs**:
   - price rising with OI rising
   - funding turned negative (shorts pay and the trade is crowded)
   - funding moved to 1h settlement
   - a bullish catalyst in the news
   Any of these: propose cutting or tightening.
4. **Targets**: the guardian executes these only if `auto.targets` is set; otherwise it alerts. When a target is reached, propose the partial close from the plan with `of_original` (`--of-original`): the fraction is then of the ORIGINAL size, as the plan counts it, so a target the guardian already took closes nothing more (STX 2026-10-01).
5. **Time stop**: past `time_stop_hours` with no follow-through means exit (the playbook says a dead short is capital at risk for nothing).
6. **Profit protection**: breakeven and trailing are automatic when their auto flags are on. Otherwise suggest `move_stop`.
7. **Update the plan when the facts change**, e.g. extend a time stop that is working, or add a target. Use `update_plan` (needs approval) and write down why.

## After the trade closes
- The PnL in the journal comes from Binance income (fees and funding included). Add the `lesson` when closing.
- Compare MAE/MFE with the plan:
  - Did the stop sit too close (MAE near the stop, then it worked)?
  - Were targets too greedy (MFE well past them, then it reversed)?
  Those are candidate learnings.

## Ladder (shadow mode, docs/LADDER.md)
- Trades in the live-ladder experiment (`plan.ladder_live == "v1"`, docs/LADDER.md): run `shortbot ladder COIN --dry-run` at each review, then `--apply` when it proposes a cut, add or re-level, and log why. Never size above the pinned 1R and never add to a loser (the code refuses).
- Run `position_ladder` (`shortbot ladder COIN`). It shows T1/T2/T3 with no-edge vs edge-adjusted odds, the state score (squeeze, tape, breadth, time) and the exposure the rule would hold.
- It is advice until validated. The guardian logs it every 15 minutes, to be scored against hold-to-plan and a price-only placebo.
- When you act against the plan, cite the score's parts, not the P&L.

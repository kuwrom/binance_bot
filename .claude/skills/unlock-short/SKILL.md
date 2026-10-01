---
name: unlock-short
description: Playbook for shorting 2-4 weeks into a large team/cliff token unlock and exiting around the unlock; unlock dates are gathered manually (no free API).
when_to_use: The user mentions a token unlock, vesting cliff, or asks to research upcoming unlocks.
---

# unlock (experimental)

**Thesis:** Keyrock's study of 16k+ unlock events: about 90% show negative pressure. Team unlocks averaged −25%, investor unlocks had little effect, ecosystem unlocks averaged +1.2%. Pressure starts about 30 days before and fades about 14 days after.
**Status:** experimental. There's no free unlock API on the $0 budget, so dates come from public pages (Tokenomist or CryptoRank pages via WebFetch, project docs).

## Checklist
- Unlock size ≥ 2% of circulating supply, and the recipient is the **team or cliff**, not ecosystem.
- Entry 14–28 days before the unlock, on the usual structure trigger (lower high, lost VWAP).
- Record the unlock date and source in the thesis. Set `time_stop_hours` to end about 2 days after the unlock.
- Funding not negative. Crowded pre-unlock shorts get squeezed.

## Research
Each unlock trade is a data point. After 10 or more, compare with `research_report` and decide with `add_learning` whether to promote this setup to active or retire it.

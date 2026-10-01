---
name: delisting-short
description: Playbook for shorting a Binance perp after its delisting is announced (deliveryDate set / Delisting announcement) - timing, squeeze risk, mandatory exit before settlement.
when_to_use: A candidate is tagged delisting, the guardian alerts binance_delisting, or an announcement mentions "Will Delist" or "Monitoring Tag".
---

# delisting

**Thesis:** delisting announcements and Monitoring Tags are usually followed by fast dumps (−4 to −34% in hours, from news reports only, with no systematic study).
**Status:** active but single-source. Evidence is thin, so keep size small and log everything.

## Rules
- Read the announcement itself (`latest_announcements`, then open the URL): it gives the **reduce-only time** and the **settlement time**. Put both in the plan's `kill_conditions`, and set `time_stop_hours` to end at least 2h before reduce-only.
- The first move happens within minutes, and we're not fast enough for it. The tradable part is the bounce-and-fail over the following hours or days.
- Expect squeezes (FLM rallied double digits after its notice). Use the wide stop and 1× leverage.
- Monitoring Tag ≠ delisting. It's a softer signal: smaller size, and use `exp_monitoring_tag` as the setup until the data supports registering it.

## Default plan
- time_stop_hours 72, which **must** be shortened to the settlement schedule
- breakeven at +5%
- review every 4h

from shortbot.sweeps import detect, summarize, tier_of


def bars_from(highs_closes, start=0):
    return [[start + i * 60_000, c, h, min(c, h) - 0.008, c, 100.0 if i != 70 else 500.0]
            for i, (h, c) in enumerate(highs_closes)]


def test_tiers():
    assert tier_of(0.60, 0.005) == 3 and tier_of(0.61, 0.005) == 2 and tier_of(0.605, 0.005) == 1


def test_fakeout_measured_until_failure():
    seq = [(0.590, 0.589)] * 70                      # quiet below 0.60 for 70 min
    seq += [(0.6004, 0.5988)]                        # poke 0.60, close back under
    seq += [(0.603, 0.602)] * 5 + [(0.6116, 0.6058)]  # accept above, spike
    seq += [(0.605, 0.604)] * 5 + [(0.600, 0.580)] * 250   # fail hard
    ev = [e for e in detect(bars_from(seq), bars_from(seq)) if e["kind"] == "round" and e["level"] == 0.6]
    assert len(ev) == 1
    e = ev[0]
    assert abs(e["fakeout_pct"] - (0.6116 / 0.6 - 1) * 100) < 1e-3 and e["rev240"] and e["vol_mult"] == 5.0
    assert e["bars_to_reclose"] == 0                 # first bar closed back under: counted, but not the whole story


def test_continuation_has_no_fakeout():
    seq = [(0.590, 0.589)] * 70 + [(0.601 + i * 0.001, 0.601 + i * 0.001) for i in range(300)]
    ev = [e for e in detect(bars_from(seq), bars_from(seq)) if e["level"] == 0.6]
    assert ev and ev[0]["fakeout_pct"] is None and not ev[0]["rev240"]


def test_summary_groups():
    e = {"ts": 1790565900000, "kind": "round", "tier": 3, "complete": True, "overshoot_pct": 1.0, "overshoot_mark_pct": 0.5,
         "overshoot_atr": 0.4, "fakeout_pct": 2.0, "fakeout_mark_pct": 1.4, "atr1h_pct": 1.0, "bars_to_reclose": 3,
         "rev15": True, "rev60": True, "rev240": True}
    s = summarize([e] * 10)
    assert s["round_tier3"]["n"] == 10 and s["round_tier3"]["fakeout_mark_pct"]["p90"] == 1.4


def test_sweep_proof_stop_clears_measured_fakeout_and_grid_zone():
    from shortbot.sweeps import sweep_proof_stop
    g = {"round_tier3": {"p90_mark_pct": 5.0, "p90_atr": 1.8, "n": 150},
         "round_tier1-2": {"p90_mark_pct": 4.0, "p90_atr": 1.4, "n": 1200},
         "high24h": {"p90_mark_pct": 2.5, "p90_atr": 1.25, "n": 250}}
    sp = sweep_proof_stop(0.593, 0.5941, 0.01, g)          # ONDO this morning: nearest level = the 24h high
    # 0.5941 + 2.5% = 0.6090 sits just under the 0.61 round number -> pushed past 0.61 + 0.3%
    assert sp["kind"] == "high24h" and 0.61 * 1.003 < sp["price"] < 0.614
    assert sweep_proof_stop(0.593, None, 0.01, {}) is None
    sui = sweep_proof_stop(1.1807, 1.2969, 0.01, g)       # 1.20 + 4.145% = 1.2497: just under 1.25 -> push past
    assert sui["price"] > 1.25 * 1.003

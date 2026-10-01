from shortbot.profile import level_behaviour, session_behaviour, shrink


def test_shrink_trusts_small_samples_little():
    s = shrink(100.0, 5, 80.0)
    assert s["w"] == round(5 / 35, 2) and 80 < s["value"] < 84
    assert shrink(100.0, 300, 80.0)["value"] > 97
    assert shrink(None, 0, 80.0)["value"] == 80.0


def test_level_behaviour_blends_coin_with_universe():
    ev = [{"kind": "high24h", "complete": True, "rev240": True, "fakeout_mark_pct": 1.0, "symbol": "A"}] * 90
    ev += [{"kind": "high24h", "complete": True, "rev240": False, "fakeout_mark_pct": 5.0, "symbol": "X"}] * 10
    out = level_behaviour(ev, "X")["high24h"]["failed_within_4h_pct"]
    assert out["coin"] == 0.0 and out["universe"] == 90.0 and 60 < out["value"] < 70


def test_session_behaviour_shares_sum_to_100():
    c = [[1790553600000 + i * 3_600_000, 1, 1, 1, 1 + (i % 3) * 0.01, 1000] for i in range(200)]
    s = session_behaviour(c)
    assert abs(sum(v["move_share_pct"] for v in s.values()) - 100) < 0.5

from shortbot import scan_job as S


def cand(sym, score=40, setups=("pump_fade",), stance="fade", vetoes=()):
    return {"symbol": sym, "score": score, "setups": list(setups), "tape": {"stance": stance},
            "vetoes": list(vetoes), "change_24h": 25.0}


def test_alerts_need_score_setup_entry_stance_and_a_non_hostile_regime():
    seen = {}
    cs = [cand("A"), cand("B", score=29), cand("C", setups=()), cand("D", stance="stand_aside"),
          cand("E", vetoes=("thin",))]
    assert [a["symbol"] for a in S.pick_alerts(cs, "caution", seen, 0)] == ["A"]
    assert S.pick_alerts([cand("F")], "hostile", {}, 0) == []
    assert "run a trading session" in S.pick_alerts([cand("G")], "favorable", {}, 0)[0]["text"]


def test_dedup_renotifies_after_two_hours_or_a_score_jump():
    seen = {}
    assert S.pick_alerts([cand("A", 40)], "caution", seen, 0)
    assert S.pick_alerts([cand("A", 45)], "caution", seen, 600) == []            # same symbol, small rise
    assert S.pick_alerts([cand("A", 50)], "caution", seen, 1200)                 # +10 since the last alert
    assert S.pick_alerts([cand("A", 50)], "caution", seen, 1200 + S.RENOTIFY_S)  # two hours later


async def test_run_logs_a_both_direction_scan_and_returns_alerts(monkeypatch):
    calls = {}

    async def fake_regime(ex):
        return {"verdict_for_shorts": "caution"}

    async def fake_scan(ex, **kw):
        calls.update(kw)
        return {"candidates": [cand("A")], "logged_for_research": 22}
    import shortbot.scanner as sc
    monkeypatch.setattr(sc, "market_regime", fake_regime)
    monkeypatch.setattr(sc, "scan_shorts", fake_scan)
    out = await S.run(object(), "J", {}, now_s=0)
    assert calls["direction"] == "both" and calls["journal"] == "J" and calls["regime"] == "caution"
    assert out["logged"] == 22 and [a["symbol"] for a in out["alerts"]] == ["A"]

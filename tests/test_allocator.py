"""Book allocator: which candidates to work next, given the book (allocator.py, learning #30)."""

from shortbot.allocator import evidence_rank, plan, setup_of


def rd(sym, playbook="pump-fade", score=20, notes=(), action="short_setup", sector="AI", size=1.0):
    return {"symbol": sym, "score": score, "numbers": {"sector": sector},
            "verdict": {"action": action, "playbook": playbook, "size_mult": size, "notes": list(notes),
                        "why": ["post_pump x hype_fade"]}}


def test_live_evidence_outranks_score():
    live = rd("A", score=5, notes=["registry: evidence-backed (live) strategy supports this"])
    shadow = rd("B", score=40, notes=["registry: hypothesis only (shadow), no live evidence: conviction C"])
    prob = rd("C", score=90, notes=["registry: strategy on PROBATION (edge fading or contradicted)"])
    assert [r["symbol"] for r in sorted([shadow, prob, live], key=evidence_rank, reverse=True)] == ["A", "B", "C"]
    out = plan([shadow, prob, live], [], 5)
    assert [a["symbol"] for a in out["add"]] == ["A", "B", "C"] and out["add"][0]["evidence"] == "live"


def test_slots_four_and_five_take_only_event_setups():
    book = [{"symbol": f"H{i}", "setup": "pump_fade", "sector": f"S{i}"} for i in range(3)]
    out = plan([rd("P", sector="X"), rd("D", playbook="delisting-short", sector="Y")], book, 5)
    assert [a["symbol"] for a in out["add"]] == ["D"] and out["add"][0]["slot"] == 4
    assert "slots 4-5 are for event setups" in out["skip"][0]["why"]


def test_full_book_held_symbols_and_non_setups_are_skipped():
    book = [{"symbol": f"H{i}", "setup": "pump_fade", "sector": "Z"} for i in range(5)]
    out = plan([rd("N", playbook="listing-fade")], book, 5)
    assert out["free_slots"] == 0 and out["add"] == [] and "no free slot" in out["skip"][0]["why"]
    out = plan([rd("H0"), rd("W", action="stand_aside")], book[:1], 5)
    whys = sorted(s["why"] for s in out["skip"])
    assert whys[0].startswith("already in the book") and whys[1].startswith("read: stand_aside")


def test_a_repeated_driver_is_planned_at_half_size_on_top_of_the_read_cut():
    book = [{"symbol": "H1", "setup": "pump_fade", "sector": "AI"}]
    out = plan([rd("A", score=30), rd("B", score=20, size=0.25)], book, 5)
    a, b = out["add"]
    assert a["size_mult"] == 1.0                       # H1 + nothing else: one repeat only
    assert b["size_mult"] == 0.125 and any("driver overlap" in w for w in b["why"])   # 0.25 x 0.5


def test_setup_names_follow_the_playbook():
    assert setup_of("pump-fade (wait for trigger: rest limits above the 24h high)") == "pump_fade"
    assert setup_of("delisting-short") == "delisting" and setup_of(None) is None


def test_a_breakdown_today_is_skipped_unless_it_is_an_event_setup():
    soon = rd("SOON", sector="X")
    soon["setups_detected"] = ["breakdown"]
    delist = rd("D", playbook="delisting-short", sector="Y")
    delist["setups_detected"] = ["breakdown"]
    out = plan([soon, delist], [], 5)
    assert [a["symbol"] for a in out["add"]] == ["D"]
    assert "breakdown today" in out["skip"][0]["why"]

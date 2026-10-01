from shortbot.read import layered_evidence, playbook


def test_playbook_mapping():
    assert playbook("pumping", "pump_in_progress", ["organic"], [])["action"] == "stand_aside"
    assert playbook("post_pump", "failed_sweep", ["organic"], [])["playbook"] == "pump-fade"
    assert playbook("new_listing", "no_edge", ["insider_overhang"], [])["playbook"] == "listing-fade"
    assert playbook("delisting_risk", "no_edge", ["unlabelled"], [])["playbook"] == "delisting-short"
    p = playbook("post_pump", "post_pump_decay", ["thin_float"], [])
    assert p["size_mult"] == 0.25 and p["notes"]
    assert playbook("mature", "no_edge", ["organic"], [])["action"] == "observe"
    assert playbook("post_pump", "failed_sweep", ["organic"], ["negative funding"])["action"] == "stand_aside"


def test_layered_evidence_shrinks_thin_samples():
    row = lambda sym, stage, ret: {"symbol": sym, "short_ret_24h": ret, "max_adverse_24h": 1.0,  # noqa: E731
                                   "features": {"lifecycle_stage": stage, "pump_state": "decaying"}}
    rows = [row("A", "mature", 0.0)] * 100 + [row("B", "post_pump", 5.0)] * 30 + [row("X", "post_pump", -10.0)] * 2
    ev = layered_evidence(rows, "X", "post_pump|post_pump_decay")
    e = ev["avg_short_ret_24h"]
    assert ev["n"]["coin_in_archetype"] == 2 and e["coin"] == -10.0 and e["estimate"] > 0   # 2 trades don't flip it


def test_registry_gate_retired_probation_shadow_live():
    from shortbot.read import gate_by_registry
    v = {"action": "short_setup", "playbook": "pump-fade", "size_mult": 1.0, "notes": [], "why": ["x"]}
    lf = {**v, "playbook": "listing-fade"}
    card = lambda i, st: {"id": i, "status": st, "reason": "r"}  # noqa: E731
    out = gate_by_registry(v, "mature", "failed_sweep", [], {"nodes": [card("strategy.level_fade", "retired")]})
    assert out["action"] == "observe" and out["size_mult"] == 0.0
    out = gate_by_registry(v, "post_pump", "post_pump_decay", [],
                           {"nodes": [card("strategy.post_pump_decay", "probation"),
                                      card("strategy.pump_fade", "probation")]})
    assert out["action"] == "short_setup" and out["size_mult"] == 0.5
    out = gate_by_registry(v, "pumping", "no_edge", [], {"nodes": [card("strategy.pump_fade", "shadow")]})
    assert out["size_mult"] == 1.0 and any("hypothesis" in n for n in out["notes"])
    out = gate_by_registry(lf, "new_listing", "no_edge", [], {"nodes": [card("strategy.listing_fade", "live")]})
    assert any("evidence-backed" in n for n in out["notes"])
    # the chosen playbook decides: a live pump_fade card must NOT back a listing-fade call
    out = gate_by_registry(lf, "new_listing", "no_edge", ["pump_fade"],
                           {"nodes": [card("strategy.pump_fade", "live"), card("strategy.listing_fade", "probation")]})
    assert out["size_mult"] == 0.5 and not any("evidence-backed" in n for n in out["notes"])
    assert gate_by_registry({"action": "observe"}, "x", "y", [], {"nodes": []}) == {"action": "observe"}


def test_failed_sweep_on_a_pumped_coin_is_judged_by_pump_fade_not_level_fade():
    from shortbot.read import gate_by_registry
    v = {"action": "short_setup", "playbook": "pump-fade", "size_mult": 1.0, "notes": [], "why": ["x"]}
    reg = {"nodes": [{"id": "strategy.level_fade", "status": "retired", "reason": "r"},
                     {"id": "strategy.pump_fade", "status": "shadow", "reason": ""}]}
    assert gate_by_registry(v, "pumping", "failed_sweep", [], reg)["action"] == "short_setup"   # CRV #9 case
    assert gate_by_registry(v, "mature", "failed_sweep", [], reg)["action"] == "observe"        # plain level fade

"""`shortbot read COIN`: what game is this coin playing right now, and how do we profit (core philosophy: most
Binance coins decay; learn the SCRIPT across coins, let the coin's own history adjust it).

    lifecycle stage  x  game on the tape  x  controller label  x  session  ->  playbook + stops + evidence

Evidence is layered: all labelled candidates -> this archetype -> this coin, each level shrunk toward the one
above with w = n/(n+30), so thin samples stay humble. 'unclassified' coins get no script: observe and log."""

from __future__ import annotations

from typing import Any

from .profile import shrink
from .research import _summary, archetype_of

STAND_ASIDE_GAMES = {"pump_in_progress", "liquidation_cascade", "accepted_breakout", "spot_led_rally",
                     "attention_rising"}


def playbook(stage: str, game: str, labels: list[str], vetoes: list[str]) -> dict[str, Any]:
    """Map (stage, game, controller) to a playbook and sizing note. Rules are priors, not triggers."""
    size, notes = 1.0, []
    if "thin_float" in labels:
        size, _ = 0.25, notes.append("thin float / squeeze-prone: <= 1/4 size, stop beyond the pump high")
    elif "mm_range" in labels:
        size, _ = 0.5, notes.append("market-maker range: fade the range top only, small targets, exit on a break")
    if "insider_overhang" in labels:
        notes.append("insider supply overhang: favours shorts into unlocks / fresh-listing distribution")
    if vetoes:
        return {"action": "stand_aside", "playbook": None, "why": vetoes, "size_mult": 0.0, "notes": notes}
    if game in STAND_ASIDE_GAMES:
        return {"action": "stand_aside", "playbook": None, "why": [f"tape: {game}"], "size_mult": 0.0,
                "notes": notes}
    if stage == "delisting_risk":
        pb = "delisting-short"
    elif stage == "unlock_ahead" or game == "pre_unlock":
        pb = "unlock-short"
    elif stage == "new_listing":
        pb = "listing-fade"
    elif game in ("failed_sweep", "post_pump_decay", "hype_fade") or stage in ("post_pump", "pumping"):
        pb = "pump-fade"
    elif game in ("exhausting_push", "crowded_longs"):
        pb = "pump-fade (wait for trigger: rest limits above the 24h high)"
    elif stage == "bleeding":
        pb = "short bounces into levels (bleeding coin)"
    else:
        return {"action": "observe", "playbook": None, "size_mult": 0.0, "notes": notes,
                "why": ["unclassified: no known script fits; observe and log, don't force a trade"]}
    return {"action": "short_setup" if game not in ("exhausting_push", "crowded_longs") else "wait_for_trigger",
            "playbook": pb, "size_mult": size, "notes": notes, "why": [f"{stage} x {game}"]}


def layered_evidence(rows: list[dict], symbol: str, arch: str) -> dict[str, Any]:
    """Average 24h short return and hit rate: universe -> archetype -> coin (each shrunk to the level above)."""
    def stats(rs: list[dict]) -> dict[str, Any]:
        s = _summary(rs)
        return {"n": s.get("n", 0), "ret": s.get("avg_short_ret_24h"), "hit": s.get("hit_rate_pct")}
    u = stats(rows)
    a_rows = [r for r in rows if archetype_of(r["features"]) == arch]
    a = stats(a_rows)
    c = stats([r for r in a_rows if r["symbol"] == symbol])
    out = {}
    for k in ("ret", "hit"):
        la = shrink(a[k], a["n"], u[k])
        lc = shrink(c[k], c["n"], la["value"])
        out["avg_short_ret_24h" if k == "ret" else "hit_rate_pct"] = {
            "estimate": lc["value"], "universe": u[k], "archetype": a[k], "coin": c[k]}
    out["n"] = {"universe": u["n"], "archetype": a["n"], "coin_in_archetype": c["n"]}
    out["how_to_read"] = "short_ret > 0 = price fell. Estimate blends coin -> archetype -> universe by n/(n+30)."
    return out


async def read_coin(ex, journal, symbol: str) -> dict[str, Any]:
    from datetime import datetime, timezone

    from .scanner import coin_snapshot
    from .sessions import session_bucket
    from .sweeps import report as sweep_report
    snap = await coin_snapshot(ex, symbol)
    f = snap["features"]
    stage, labels = f.get("lifecycle_stage") or "unknown_stage", f.get("controller_labels") or ["unlabelled"]
    game = (snap.get("tape") or {}).get("state", "no_edge")
    arch = archetype_of(f)
    sess = session_bucket(datetime.now(timezone.utc))
    ses = (sweep_report(journal).get("by_session") or {}).get(sess, {})
    rows = [r for r in journal.candidates(labelled_only=True) if r.get("short_ret_24h") is not None]
    return {
        "symbol": snap["symbol"], "last": snap["last"], "score": snap["score"],
        "stage": stage, "stage_tags": f.get("lifecycle_tags"), "game": game, "controller": labels,
        "archetype": arch, "verdict": playbook(stage, game, labels, snap.get("vetoes") or []),
        "setups_detected": snap.get("setups"),
        "numbers": {k: f.get(k) for k in ("listing_age_days", "lc_ret_7d_pct", "lc_ret_30d_pct",
                                          "lc_pump_multiple_30d", "lc_off_30d_high_pct", "lc_dd_from_high_pct",
                                          "float_pct", "fdv_mc", "oi_mc", "funding_7d_avg", "sector",
                                          "resid_sector24h", "map_regime")},
        "session": {"now": sess, "levels_here": {k: {"failed_4h_pct": v.get("rev240_pct"),
                                                     "fakeout_p90_mark": v.get("fakeout_mark_p90"), "n": v.get("n")}
                                                 for k, v in ses.items()}},
        "stops": snap.get("suggested_stops"),
        "evidence": layered_evidence(rows, snap["symbol"], arch),
        "registry": (reg := registry_view(journal, stage, game, labels, snap.get("setups") or [])),
    } | {"verdict": gate_by_registry(playbook(stage, game, labels, snap.get("vetoes") or []), stage, game,
                                     snap.get("setups") or [], reg)}


STAGE_STRATEGY = {"delisting_risk": "strategy.delisting", "new_listing": "strategy.listing_fade",
                  "bleeding": "strategy.bounce_fade", "post_pump": "strategy.pump_fade", "pumping": "strategy.pump_fade"}


def relevant_strategies(stage: str, game: str, setups: list[str]) -> list[str]:
    ids = [TAPE_STRATEGY.get(game), STAGE_STRATEGY.get(stage), *(SETUP_STRATEGY.get(s) for s in setups)]
    return list(dict.fromkeys(i for i in ids if i))


def gate_by_registry(verdict: dict[str, Any], stage: str, game: str, setups: list[str],
                     reg: dict[str, Any]) -> dict[str, Any]:
    """The playbook is a prior; the registry holds the evidence. Retired strategies can't carry a trade (observe);
    probation halves size; only a 'live' card counts as evidence-backed (everything else = hypothesis, size C)."""
    if verdict.get("action") not in ("short_setup", "wait_for_trigger"):
        return verdict
    cards = {n["id"]: n for n in reg.get("nodes") or []}
    rel = [cards[i] for i in playbook_strategies(verdict.get("playbook"), game, stage) if i in cards]
    if not rel:
        return verdict
    v = {**verdict, "notes": list(verdict.get("notes") or [])}
    alive = [c for c in rel if c["status"] != "retired"]
    for c in rel:
        if c["status"] == "retired":
            v["notes"].append(f"registry: {c['id']} RETIRED ({c.get('reason') or 'no edge'})")
    if not alive:
        return {**v, "action": "observe", "size_mult": 0.0, "playbook": None,
                "why": [*v.get("why", []), "every strategy behind this playbook is retired in the registry"]}
    if any(c["status"] == "live" for c in alive):
        v["notes"].append("registry: evidence-backed (live) strategy supports this")
        return v
    if any(c["status"] == "probation" for c in alive):   # any doubt behind the chosen playbook halves size
        v["size_mult"] = round(v.get("size_mult", 1.0) * 0.5, 3)
        v["notes"].append("registry: strategy on PROBATION (edge fading or contradicted): half size, conviction C")
    else:
        v["notes"].append("registry: hypothesis only (shadow), no live evidence: conviction C")
    return v


def playbook_strategies(pb: str | None, game: str, stage: str = "") -> list[str]:
    """The strategy nodes behind the playbook `playbook()` actually CHOSE (not every node the coin touches): a
    listing-fade call is judged by listing_fade's evidence, never by an unrelated pump_fade card. A failed sweep
    on a PUMPED coin is the pump-fade trigger itself (the backtest's pump_fade = fade the failed sweep of the pump
    high, CRV #9); level_fade only covers sweeps of ordinary levels on coins that did not pump."""
    pb = pb or ""
    if pb.startswith("pump-fade"):
        if stage in ("pumping", "post_pump") and game == "failed_sweep":
            return ["strategy.pump_fade"]
        return [TAPE_STRATEGY[game]] if game in TAPE_STRATEGY else ["strategy.pump_fade"]
    return {"listing-fade": ["strategy.listing_fade"], "delisting-short": ["strategy.delisting"],
            "short bounces into levels (bleeding coin)": ["strategy.bounce_fade"]}.get(pb, [])


SETUP_STRATEGY = {"pump_fade": "strategy.pump_fade", "breakdown": "strategy.bounce_fade",
                  "listing_fade": "strategy.listing_fade", "delisting": "strategy.delisting"}
TAPE_STRATEGY = {"post_pump_decay": "strategy.post_pump_decay", "failed_sweep": "strategy.level_fade"}


def registry_view(journal, stage: str, game: str, labels: list[str], setups: list[str]) -> dict[str, Any]:
    """Coarse cut: the hierarchy nodes this coin sits in, each with its evidence card; plus matching lessons."""
    from . import registry as R
    ids = [f"lifecycle.{stage}", f"tape.{game}", *(f"controller.{c}" for c in labels),
           *(f"setup.{s}" for s in setups), *(SETUP_STRATEGY[s] for s in setups if s in SETUP_STRATEGY)]
    if game in TAPE_STRATEGY:
        ids.append(TAPE_STRATEGY[game])
    ids.append("strategy.pump_fade")   # the gate may judge a pumped coin's failed sweep by pump_fade's card
    try:
        return {"nodes": R.node_cards(journal.db, ids),
                "lessons": R.search(journal.db, " ".join([stage, game, *setups]).replace("_", " "), 5),
                "how_to_read": "status live = evidence-backed; shadow/draft = hypothesis. mean is per the metric "
                               "(trade_r in R, else short return %). 'forensics' sources are hindsight-picked."}
    except Exception as e:  # noqa: BLE001 - the registry is an aid; a read never fails because of it
        return {"error": f"{type(e).__name__}: {e}"}

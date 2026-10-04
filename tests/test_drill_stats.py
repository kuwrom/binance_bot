"""Plumbing drills' trades (setup exp_drill*, #46) are real positions but never evidence: every reader of OPEN trades
(exposure, the brief) sees them; every statistical reader of CLOSED ones skips them: closed_results (and through it
the governor's streaks, tilts, setup ladder, registry forward feed, learning scores), stats' exit quality and other-mode
counts, fill_report, the warm-up clock, the coin profile and web view, lab.trades, the ladder shadow evaluation."""

import json
import time

from shortbot import fill_report, lab, learnings_view, shadow_eval, warmup
from shortbot.journal import Journal
from shortbot.journal_tables import is_drill
from shortbot.web import research as web_research

SYM = "XRP/USDT:USDT"


def closed(j, setup, symbol=SYM, pnl=-0.05, plan=None, mode=None):
    tid = j.create_trade(symbol, setup, "t", 2.75, 2, plan=plan)
    j.activate_trade(tid, 2.5, 2.1, 5.25)
    j.close_trade(tid, 2.51, pnl, "drill done", "a lesson", fees=0.01)
    j.update_trade(tid, post_exit={"further_drop_24h_pct": 9.0, "rise_against_24h_pct": 1.0, "ret_24h_pct": 9.0},
                   **({"mode": mode} if mode else {}))
    return tid


def journal_with_both():
    j = Journal(":memory:")
    real = closed(j, "pump_fade", pnl=1.0)
    drill = closed(j, "exp_drill", pnl=-0.05)
    other = closed(j, "exp_drill_restart", pnl=-0.04)
    plain_exp, untagged = closed(j, "exp_other", pnl=0.5), closed(j, None, pnl=0.2)
    return j, {"real": real, "drill": drill, "drill2": other, "exp": plain_exp, "none": untagged}


def test_is_drill_matches_the_sql_predicate():
    assert is_drill("exp_drill") and is_drill("exp_drill_restart")
    assert not any(is_drill(s) for s in (None, "", "exp_other", "pump_fade", "exp_dril"))


def test_closed_results_and_stats_skip_drill_trades_but_keep_null_and_other_setups():
    j, ids = journal_with_both()
    assert [r["id"] for r in j.closed_results()] == [ids["real"], ids["exp"], ids["none"]]
    assert [r["id"] for r in j.closed_results(all_modes=True)] == [ids["real"], ids["exp"], ids["none"]]
    s = j.stats()
    assert s["closed_trades"] == 3 and set(s["by_setup"]) == {"pump_fade", "exp_other", "untagged"}
    assert s["exits"]["n"] == 3 and s["total_pnl"] == 1.7
    assert len(j.trades(50)) == 5 and len(j.trades(50, drills=False)) == 3   # the plain listing still shows them


def test_other_modes_count_skips_drills():
    j = Journal(":memory:", "live")
    closed(j, "exp_drill", mode="demo")
    closed(j, "pump_fade", mode="demo")
    assert j.stats()["other_modes"] == {"demo": 1}


def test_open_drill_trades_are_still_real_risk():
    j = Journal(":memory:")
    tid = j.create_trade(SYM, "exp_drill", "t", 2.75, 2)
    j.activate_trade(tid, 2.5, 2.1, 5.25)
    assert [t["id"] for t in j.open_trades()] == [tid] and j.active_trade(SYM)["id"] == tid


def test_fill_report_skips_drill_fills():
    j, ids = journal_with_both()
    for tid in ids.values():
        j.update_trade(tid, fill_refs=json.dumps({"fills": [{"leg": "entry", "type": "market", "px": 2.5}]}))
    assert {t["id"] for t in fill_report._trades(j, False)} == {ids["real"], ids["exp"], ids["none"]}


def test_a_drill_does_not_warm_a_cold_desk():
    j = Journal(":memory:")
    closed(j, "exp_drill")
    assert warmup.last_activity(j) is None
    closed(j, "pump_fade")
    assert warmup.last_activity(j) is not None


def test_learning_scores_ignore_a_drill_trade_citing_a_learning():
    j = Journal(":memory:")
    closed(j, "exp_drill", pnl=-0.5, plan={"uses_learnings": [7]})
    closed(j, "pump_fade", pnl=-0.5, plan={"uses_learnings": [8]})
    assert set(learnings_view.scores(j)) == {8}


def test_the_coin_view_lists_no_drill_trades():
    j, ids = journal_with_both()
    got = web_research.coin(j, "XRPUSDT", int(time.time() * 1000))
    assert {t["id"] for t in got["trades"]} == {ids["real"], ids["exp"], ids["none"]}


def test_lab_trades_leave_drills_out_unless_asked():
    j, ids = journal_with_both()
    lab_j = lab.journal()   # the isolated test journal (conftest): the same file
    for tid in ids.values():
        t = j.get_trade(tid)
        lab_j.create_trade(SYM, t["setup"], "t", 2.75, 2)
    assert len(lab.trades()) == 3 and len(lab.trades(drills=True)) == 5


def test_the_ladder_shadow_evaluation_skips_drill_trades():
    j, ids = journal_with_both()
    for tid in (ids["real"], ids["drill"]):
        for i, px in enumerate((2.5, 2.4, 2.45)):
            j.db.execute("INSERT INTO ladder_shadow (ts, trade_id, symbol, price, x_actual, x_virtual, init_r, variant)"
                         " VALUES (?,?,?,?,?,?,?,?)", (f"2026-10-04T00:0{i}:00", tid, SYM, px, 1.0, 1.0, 0.25, "base"))
    j.db.commit()
    assert set(shadow_eval.evaluate(j)["per_trade_R"]) == {ids["real"]}

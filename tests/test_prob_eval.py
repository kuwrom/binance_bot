from shortbot.journal import Journal
from shortbot.probability.evaluate import brier, expectancy, report
from shortbot.probability.labels import label_bracket, label_touch
from shortbot.research_stats import wilson


def c(h, lo, close=None):
    return [0, 0, h, lo, close if close is not None else (h + lo) / 2, 1]


def test_label_bracket_order_and_conservative_same_bar():
    assert label_bracket([c(1.01, 0.99), c(1.02, 0.95)], 1.0, 1.05, 0.95)["outcome"] == "target"
    assert label_bracket([c(1.06, 0.99)], 1.0, 1.05, 0.95)["outcome"] == "stop"
    assert label_bracket([c(1.06, 0.94)], 1.0, 1.05, 0.95)["outcome"] == "stop"      # ambiguous -> stop first
    t = label_bracket([c(1.02, 0.98, 0.99)], 1.0, 1.05, 0.95)
    assert t["outcome"] == "time" and t["mae_r"] == 0.4 and t["mfe_r"] == 0.4
    assert label_touch([c(1.02, 0.99)], 1.015, above=True)["outcome"] == "hit"


def test_wilson_and_brier():
    lo, hi = wilson(30, 100)
    assert lo < 0.3 < hi
    rows = [{"p": 0.3, "_y": i < 3} for i in range(10)]
    b = brier(rows, "p")
    assert b["n"] == 10 and b["reliability"] < 1e-9


def test_expectancy_edge_detection():
    rows = []
    for i in range(200):
        rows.append({"entry": 1.0, "stop": 1.05, "target": 0.90, "p_model": 0.333,
                     "outcome": "target" if i % 2 == 0 else "stop", "_y": i % 2 == 0, "context": {"setup": "a"}})
    e = expectancy(rows, lambda r: r["context"]["setup"])["a"]
    assert e["p_target_observed"] == 0.5 and e["edge_significant"] and e["edge_pp"] > 15
    assert e["avg_R_ex_time"] == 0.5   # 0.5*2R - 0.5*1R


def test_report_with_journal():
    j = Journal(":memory:")
    pid = j.log_prediction("candidate", "X/USDT:USDT", 1.0, 1.05, 0.90, 24, "bracket", 0.33, p_analyst=0.45,
                           context={"setup": "pump_fade", "tape_state": "failed_sweep", "regime": "caution"}, ts_ms=1)
    assert j.due_predictions(now_ms=10**13)[0]["id"] == pid
    j.label_prediction(pid, "target", 2.1, 0.3)
    r = report(j)
    assert r["labelled_brackets"] == 1 and r["model_calibration"]["n"] == 1

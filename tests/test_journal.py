import sqlite3
import time

import pytest

from shortbot.journal import Journal


def test_trade_lifecycle_stats_and_r_multiple():
    j = Journal(":memory:")
    sid = j.start_session("demo", "first session")
    j.log_decision("skip", "funding negative, crowded", symbol="AAA/USDT:USDT", snapshot={"f": -0.001},
                   session_id=sid)
    tid = j.create_trade("BBB/USDT:USDT", "pump_fade", "lower high", stop_price=2.2, leverage=2,
                         plan={"time_stop_hours": 24}, entry_features={"rsi_1h": 88})
    assert j.active_trade("BBB/USDT:USDT")["status"] == "opening" and j.open_trades() == []
    j.activate_trade(tid, 2.0, 50, 100)
    t = j.open_trades()[0]
    assert t["plan"]["time_stop_hours"] == 24 and t["entry_features"]["rsi_1h"] == 88 and t["initial_stop"] == 2.2
    j.update_trade(tid, stop_price=2.1, targets_hit=[1.8])
    assert j.get_trade(tid)["targets_hit"] == [1.8] and j.get_trade(tid)["initial_stop"] == 2.2
    j.close_trade(tid, 1.8, 9.5, "target", "worked", gross_pnl=10.0, fees=-0.3, funding=-0.2)
    s = j.stats()
    assert s["closed_trades"] == 1 and s["by_setup"]["pump_fade"]["win_rate"] == 100.0
    assert s["by_setup"]["pump_fade"]["avg_r"] == pytest.approx(0.95)   # 9.5 / (0.2 * 50)
    assert j.decisions(symbol="AAA/USDT:USDT")[0]["action"] == "skip"


def test_abort_and_unknown_field():
    j = Journal(":memory:")
    tid = j.create_trade("X", "pump_fade", "t", 1.1, 2)
    j.abort_trade(tid, "entry failed")
    assert j.active_trade("X") is None and j.get_trade(tid)["status"] == "aborted"
    with pytest.raises(ValueError):
        j.update_trade(tid, not_a_column=1)


def test_migrates_old_schema(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, status TEXT NOT NULL "
                "DEFAULT 'open', opened_at TEXT NOT NULL, entry_price REAL, pnl REAL, setup TEXT)")
    con.execute("INSERT INTO trades (symbol, opened_at, entry_price) VALUES ('OLD', 'x', 1.0)")
    con.commit()
    con.close()
    j = Journal(db)
    assert j.open_trades()[0]["symbol"] == "OLD" and "plan" in j.open_trades()[0]


def test_candidates_and_learnings():
    j = Journal(":memory:")
    n = j.log_candidates("2026-09-27T00:00:00+00:00", 1000, [
        {"symbol": "A", "price": 1.0, "score": 50, "vetoes": [], "signals": ["+10 x"], "features": {"rsi_1h": 85}},
        {"symbol": "B", "price": 2.0, "score": 20, "vetoes": ["thin"], "features": {}}], "caution")
    assert n == 2 and len(j.unlabelled_candidates(older_than_ms=2000)) == 2
    cid = j.unlabelled_candidates(2000)[0]["id"]
    j.label_candidate(cid, {"short_ret_24h": 5.0, "max_adverse_24h": 3.0})
    lab = j.candidates(labelled_only=True)
    assert len(lab) == 1 and lab[0]["features"]["rsi_1h"] == 85 and lab[0]["short_ret_24h"] == 5.0
    lid = j.add_learning("RSI>85 fades within 24h", "12 candidates", setup="pump_fade")
    j.update_learning(lid, status="supported", evidence="30 more", confidence="medium")
    le = j.learnings()[0]
    assert le["status"] == "supported" and "30 more" in le["evidence"]
    with pytest.raises(ValueError):
        j.add_learning("x", status="maybe")


def test_metrics_alerts_and_kv_prune():
    j = Journal(":memory:")
    j.record_metrics([("2026-09-27T00:00:00+00:00", "X", 1.0, 0.0001, 1e6, 3e7)])
    assert j.metric_history("X")[0]["funding_rate"] == 0.0001
    j.log_alert("squeeze", "OI +20% price +8%", "X")
    assert j.alerts()[0]["kind"] == "squeeze"
    j.set("news_seen:old", str(time.time() - 90 * 86400))
    j.set("news_seen:new", str(time.time()))
    assert j.prune_kv(("news_seen:",), 30 * 86400) == 1 and j.get("news_seen:new")

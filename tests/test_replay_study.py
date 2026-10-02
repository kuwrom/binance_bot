"""Stage-2 replay study (backtest/replay_study.py, #73): scan a window with the real scanner, label, delay table,
trial register, provenance."""

import json

from shortbot import trial_register
from shortbot.backtest import replay_study as RS
from shortbot.backtest.replay_world import iso
from shortbot.journal import Journal
from tests import replay_fakes as F


def test_scan_label_and_report_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTBOT_TRIALS_DB", str(tmp_path / "trials.db"))
    work = tmp_path / "work"
    s = F.build(work / "cache.db")
    s.derive_five("perp")
    Journal(work / "snap.db").db.close()                   # an empty desk journal (no risk-param changes)
    since, until = iso(F.T0 + 26 * F.HOUR), iso(F.T0 + 30 * F.HOUR)
    common = ["--work", str(work), "--since", since, "--until", until, "--cadence", "60"]
    assert RS.main(["scan", *common, "--chunk", "0/2"]) == 0
    assert RS.main(["scan", *common, "--chunk", "1/2"]) == 0
    assert RS.main(["scan", *common, "--chunk", "1/2"]) == 0                 # resumes: nothing left to scan
    out = tmp_path / "out"
    assert RS.main(["report", *common, "--out", str(out)]) == 0
    res = json.loads((out / "results.json").read_text())
    assert res["config"]["scans"] == 4 and res["config"]["candidates"] >= 4
    cands = RS.candidates(work / "merged.db")
    assert len({c["ts_ms"] for c in cands}) == 4 and all(c["features"]["scan"]["direction"] == "both" for c in cands)
    assert (out / "provenance.json").exists() and "R by entry delay" in (out / "report.md").read_text()
    assert trial_register.count() == sum(1 for r in res["delays"] if r["n"])
    # the synthetic coins trade below the 20M floor, so force one tradable pump_fade to exercise the delay path
    aaa = [{**c, "tradable": 1, "setups": ["pump_fade"]} for c in cands if c["symbol"] == "BBB/USDT:USDT"]
    got = RS.delays(s, aaa)
    assert list(got) == ["pump_fade"] and len(got["pump_fade"]) == 1               # one episode for 4 scans
    sims = got["pump_fade"][0][1]
    assert set(sims) == {0, 5, 15, 30} and sims[0]["filled"] and sims[30]["entry_ts"] > sims[0]["entry_ts"]


def test_times_cover_the_window_at_the_cadence():
    assert RS.times(0, 3 * 3_600_000, 30) == [k * 1_800_000 for k in range(6)]

"""Logged scan vs replayed scan (backtest/replay_compare.py): universe, decisions, match rate (task #29)."""

from shortbot.backtest import replay_compare as RC

DAY = 86_400_000
T = 1_790_208_000_000


def r(sym, tradable=True, setups=("pump_fade",), vetoes=(), tape="no_edge", score=30, **f):
    return {"symbol": sym, "tradable": int(tradable), "setups": list(setups), "vetoes": list(vetoes), "score": score,
            "features": {**({"tape_state": tape} if tape is not None else {}), **f}}


def test_compare_scan_rows_universe_and_features():
    logged = [r("A", change_24h=20.0), r("B", tradable=False, vetoes=["24h volume $3.1M below floor"]),
              r("C", tape=None), r("D")]
    replayed = [r("A", change_24h=20.2), r("B", tradable=False, vetoes=["24h volume $3.4M below floor"]),
                r("C", tape="hype_fade"), r("E")]
    c = RC.compare_scan(logged, replayed)
    assert c["universe"] == {"logged": 4, "replayed": 4, "common": 3, "recall": 0.75, "precision": 0.75,
                             "jaccard": 0.6, "missing": ["D"], "extra": ["E"]}
    rows = {x["symbol"]: x for x in c["rows"]}
    assert rows["A"]["match"] and rows["B"]["match"] and rows["B"]["vetoes"]       # numbers in vetoes don't count
    assert rows["C"]["match"] and rows["C"]["tape"] is None                         # no logged tape state: skipped
    assert not rows["D"]["match"] and not rows["D"]["in_replay"]
    assert c["tradable"]["jaccard"] == 2 / 4
    fa = next(f for f in c["features"] if f["symbol"] == "A")
    assert fa["change_24h"] == (20.0, 20.2) and fa["tape_state"] == ("no_edge", "no_edge")


def test_mismatches_are_attributed():
    c = RC.compare_scan([r("A", tradable=False, vetoes=["pump in progress (1h return > 5%)"], tape="pump_in_progress")],
                        [r("A", tape="no_edge")])
    x = c["rows"][0]
    assert not x["match"] and not x["tradable"] and x["vetoes_logged_only"] == ["pump in progress"]
    assert x["tape_pair"] == ["pump_in_progress", "no_edge"]


def test_summary_clusters_by_day():
    res = [{"ts_ms": T + d * DAY + k, "cmp": RC.compare_scan([r("A"), r("B")], [r("A"), r("B", tape="x")])}
           for d in range(3) for k in (0, 1)]
    s = RC.summarize(res)
    assert s["scans"] == 6 and s["rows"] == 12
    assert s["match"]["p"] == 0.5 and s["match"]["days"] == 3 and s["agree_tradable"]["p"] == 1.0
    assert s["tape_confusion"] == [("no_edge -> x", 6)]
    assert s["features"]["tape_state"] == {"n": 12, "agree": 0.5}
    assert s["universe_exact_share"] == 1.0 and s["tradable_jaccard_mean"] == 1.0

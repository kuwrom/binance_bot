"""replay_verify (task #29): a scan logged by the live pipeline replays to itself through the real scanner."""

import asyncio
import json

from shortbot import clock
from shortbot.backtest import replay_verify as RV
from shortbot.backtest import replay_world as W
from shortbot.backtest.replay_exchange import ReplayExchange
from shortbot.journal import Journal
from shortbot.scanner import scan_shorts
from tests import replay_fakes as F


def test_map_for_uses_only_fresh_maps():
    t = F.T
    assert RV.map_for([t // 1000 - 100, t // 1000 + 5], t) == t // 1000 - 100
    assert RV.map_for([t // 1000 - 4000], t) is None
    assert RV.map_for([], t) is None


def _live_journal(tmp_path, store):
    """A 'live' journal whose scan was produced by the same pipeline on the same data (ground truth by construction)."""
    live = tmp_path / "live.db"
    j = Journal(live)
    with W.offline_feeds():
        async def go():
            with clock.at(F.T):
                await scan_shorts(ReplayExchange(store, universe="demo", db_path=live), min_change=5, depth=25,
                                  journal=j, regime="neutral", direction="both")
        asyncio.run(go())
    j.db.close()
    return live


def test_cli_replays_a_logged_scan_to_itself(tmp_path, capsys):
    store = F.build(tmp_path / "work" / "cache.db")
    live = _live_journal(tmp_path, store)
    out = tmp_path / "out"
    assert RV.main(["--journal", str(live), "--work", str(tmp_path / "work"), "--since", "2026-09-26",
                    "--until", "2026-09-27", "--out", str(out)]) == 0
    res = json.loads((out / "results.json").read_text())
    assert res["summary"]["code_since"] == "2026-09-26T14:36:40+00:00"
    assert res["summary"]["universe"]["missing_ranked_out"] == 0
    s = res["summary"]["current_code"]
    assert res["summary"]["all"]["rows"] == 2
    assert s["scans"] == 1 and s["rows"] == 2 and s["match"]["p"] == 1.0 and s["universe_exact_share"] == 1.0
    assert res["scans"][0]["args"] == {"direction": "both", "min_change": 5, "depth": 25, "top_n": 10,
                                       "groups": None, "inferred": False}
    assert s["features"]["change_24h"]["within_1pct"] == 1.0
    prov = json.loads((out / "provenance.json").read_text())
    assert prov["config"]["command"] == "shortbot.backtest.replay_verify" and "journal_snapshot" in prov["inputs"]
    assert "row match" in (out / "report.md").read_text()
    assert "current_code: match 100.0%" in capsys.readouterr().out

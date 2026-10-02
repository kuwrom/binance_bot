"""params_backfill (#32): limit fills journalled without their experiment get the one active at PLACEMENT; dry run is
read-only; apply is idempotent and never touches a trade that already has an experiment."""

import json
import sqlite3

from shortbot import params_backfill as pb
from shortbot.config import RiskLimits
from shortbot.entries import SCHEMA as ENTRIES
from shortbot.journal import Journal

LEV = {**RiskLimits().__dict__, "max_leverage": 10, "max_position_notional": 1000.0}
LIQ = {**LEV, "min_quote_volume_24h": 5e6}


def fixture(tmp_path):
    """The demo journal's shape on 2026-10-01: legacy set_params snapshots (tag only), limit fills without one."""
    db = tmp_path / "shortbot.db"
    j = Journal(db)
    j.db.executescript(ENTRIES)
    for ts, action, snap in (("2026-09-27T13:59:23+00:00", "set_params", {"experiment": "lev10_risk3", "after": LEV}),
                             ("2026-09-29T18:15:08+00:00", "set_params", {"experiment": "lev10_risk3+liq_tiers",
                                                                          "after": LIQ}),
                             ("2026-10-01T20:00:00+00:00", "reset_params", {"after": RiskLimits().__dict__})):
        j.db.execute("INSERT INTO decisions (ts, action, reasoning, snapshot) VALUES (?,?,?,?)",
                     (ts, action, "r", json.dumps(snap)))
    ids = {}
    for name, placed, mode, exp in (("before", "2026-09-27T13:00:00+00:00", "demo", None),
                                    ("lev", "2026-09-28T01:45:06+00:00", "demo", None),
                                    ("liq", "2026-10-01T08:45:07+00:00", "demo", None),
                                    ("after_reset", "2026-10-01T21:00:00+00:00", "demo", None),
                                    ("tagged", "2026-09-28T02:00:00+00:00", "demo", "x"),
                                    ("live", "2026-09-28T03:00:00+00:00", "live", None)):
        tid = j.create_trade("A/USDT:USDT", "pump_fade", "t", 1.1, 3, entry_features={"entry_type": "limit"},
                             experiment=exp)
        j.update_trade(tid, mode=mode)
        j.db.execute("INSERT INTO entries (group_id, symbol, status, created_at, expires_ms, trade_id) "
                     "VALUES ('g', 'A/USDT:USDT', 'filled', ?, 0, ?)", (placed, tid))
        ids[name] = tid
    j.db.commit()
    return db, j, ids


def test_dry_run_plans_from_placement_time_and_writes_nothing(tmp_path, capsys):
    db, j, ids = fixture(tmp_path)
    assert pb.main(["--db", str(db)]) == 0
    out = json.loads(capsys.readouterr().out)
    got = {r["trade_id"]: (r["experiment"], r["limits"]["min_quote_volume_24h"]) for r in out["rows"]}
    assert out["dry_run"] and got == {ids["lev"]: ("lev10_risk3", RiskLimits().min_quote_volume_24h),
                                      ids["liq"]: ("lev10_risk3+liq_tiers", 5e6)}
    assert all(j.get_trade(i)["experiment"] is None for i in (ids["lev"], ids["liq"]))


def test_apply_tags_once_and_keeps_existing_tags(tmp_path, capsys):
    db, j, ids = fixture(tmp_path)
    assert pb.main(["--db", str(db), "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] == 2
    j2 = Journal(db)
    t = j2.get_trade(ids["liq"])
    assert t["experiment"] == "lev10_risk3+liq_tiers" and t["entry_features"]["limits"] == LIQ
    assert t["entry_features"]["entry_type"] == "limit" and t["entry_features"]["attribution_backfill"]
    assert j2.get_trade(ids["tagged"])["experiment"] == "x"
    assert all(j2.get_trade(ids[k])["experiment"] is None for k in ("before", "after_reset", "live"))
    assert j2.decisions()[0]["action"] == "experiment_backfill"
    assert pb.plan(sqlite3.connect(db)) == []                       # idempotent


def test_new_style_snapshots_use_the_active_label(tmp_path):
    db, j, ids = fixture(tmp_path)
    j.db.execute("INSERT INTO decisions (ts, action, reasoning, snapshot) VALUES (?,?,?,?)",
                 ("2026-09-28T00:00:00+00:00", "set_params", "r",
                  json.dumps({"experiment": "b", "active": "a + b", "after": LEV, "mode": "demo"})))
    j.db.commit()
    rows = {r["trade_id"]: r["experiment"] for r in pb.plan(sqlite3.connect(db))}
    assert rows[ids["lev"]] == "a + b"

"""#31: one append-only trial register for every study (data/research/trials.db)."""

import hashlib
import json
import random
import sqlite3

import pytest

from shortbot import trial_register as TR
from shortbot.backtest import trials_eff


@pytest.fixture
def reg(tmp_path, monkeypatch):
    p = tmp_path / "research" / "trials.db"
    monkeypatch.setenv(TR.ENV, str(p))
    monkeypatch.delenv(TR.ASOF_ENV, raising=False)
    return p


def _row(name, mean=0.1, daily=None, **kw):
    return {"name": name, "params": {"trial": name}, "family": "strategy.x", "n": 50, "mean": mean, "t": 1.0,
            "t_kind": "2way", "daily": daily, **kw}


def test_a_missing_register_reads_as_empty_and_is_not_created(reg):
    assert TR.summary() == {"rows": 0, "specs": 0, "controls": 0, "by_study": {}, "max_id": 0}
    assert TR.others() == ({}, 0) and TR.count("strategy.x") == 0
    assert not reg.exists()


def test_record_creates_on_demand_and_the_register_is_append_only(reg):
    assert TR.record("backtest", [_row("a"), _row("b")]) == 2
    assert reg.exists()
    db = sqlite3.connect(reg)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE trials SET mean = 9")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM trials")


def test_reruns_count_once_and_controls_never(reg):
    TR.record("backtest", [_row("a"), _row("b")])
    TR.record("backtest", [_row("a", mean=0.2)])                       # a rerun of the same spec
    TR.record("rungs", [_row("random|x", control=True)])
    s = TR.summary()
    assert s["rows"] == 4 and s["specs"] == 2 and s["controls"] == 1 and s["by_study"] == {"backtest": 2}
    assert TR.count("strategy.x") == 2


def test_provenance_is_stamped(reg):
    prov = {"git": {"sha": "abc1234def", "dirty": False}, "code_sha": "c0de", "window": {"t0": "2026-07-27"}}
    TR.record("hedge", [_row("btc_1")], prov=prov, run_dir="data/research/x/hedge")
    r = sqlite3.connect(reg).execute("SELECT git_sha, code_sha, run_dir, window FROM trials").fetchone()
    assert r[:3] == ("abc1234def", "c0de", "data/research/x/hedge") and json.loads(r[3]) == {"t0": "2026-07-27"}


def _journal(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE pattern_trials (id INTEGER PRIMARY KEY, pattern_id TEXT, version INTEGER, params TEXT, source TEXT,
            n INTEGER, n_coins INTEGER, mean REAL, t REAL, oos_n INTEGER, oos_mean REAL, created_at TEXT);
        CREATE TABLE trial_ledger (variant TEXT PRIMARY KEY, family TEXT, params TEXT, created TEXT);""")
    db.executemany("INSERT INTO pattern_trials (pattern_id, version, params, source, n, mean, t, created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   [("strategy.x", 1, json.dumps({"trial": "a", "k": 1}, sort_keys=True), "bt60", 9, 0.1, 2.0, "t"),
                    ("lib.w", 1, json.dumps({"group": "random", "variant": "v"}), "rungs_60d", 9, 0.0, 0.1, "t"),
                    ("lib.c", 1, json.dumps({"regime": "top", "outcome": "o"}), "carry_regime_daily", 9, 0.1, 1.0, "t")])
    db.executemany("INSERT INTO trial_ledger VALUES (?,?,?,?)",
                   [("base", "ladder", "{}", "t"), ("placebo", "ladder", json.dumps({"placebo": True}), "t")])
    db.commit()
    db.close()


def test_legacy_ledgers_are_migrated_once_and_read_only(reg, tmp_path):
    j = tmp_path / "shortbot.db"
    _journal(j)
    before = hashlib.sha256(j.read_bytes()).hexdigest()
    out = TR.migrate_legacy(j)
    assert out == {"pattern_trials": 3, "trial_ledger": 2, "added": 5}
    assert TR.migrate_legacy(j)["added"] == 0                          # idempotent
    assert hashlib.sha256(j.read_bytes()).hexdigest() == before        # the journal is only read
    s = TR.summary()
    assert s["controls"] == 2 and s["by_study"] == {"backtest": 1, "carry_regime": 1, "ladder": 1}
    # a legacy bt60 row is the same spec as the backtest's own record of that trial: no double count
    TR.record("backtest", [{"name": "a", "family": "strategy.x", "params": {"trial": "a", "k": 1}, "n": 9}])
    assert TR.summary()["specs"] == 3


def test_migration_from_a_journal_without_the_tables(reg, tmp_path):
    j = tmp_path / "empty.db"
    sqlite3.connect(j).close()
    assert TR.migrate_legacy(j) == {"pattern_trials": 0, "trial_ledger": 0, "added": 0}
    assert TR.migrate_legacy(tmp_path / "missing.db")["added"] == 0


def _series(rng, days, base=None, noise=1.0):
    return {d: (base[d] if base else 0) + rng.gauss(0, noise) for d in days}


def test_others_feed_the_effective_number_of_trials(reg, monkeypatch):
    rng = random.Random(3)
    days = [f"2026-08-{k:02d}" for k in range(1, 29)]
    run_book = [{"r": rng.gauss(0, 1), "day": d} for d in days for _ in range(3)]
    run_daily = {}
    for t in run_book:
        run_daily[t["day"]] = run_daily.get(t["day"], 0) + t["r"]
    TR.record("hedge", [_row("clone", daily=_series(rng, days, run_daily, 0.01)),
                        _row("indep", daily=_series(rng, days)), _row("no_series")])
    others, bare = TR.others(exclude={TR.spec_of("backtest", "strategy.x", {"trial": "run"})})
    assert set(others) == {TR.spec_of("hedge", "strategy.x", {"trial": "clone"}),
                           TR.spec_of("hedge", "strategy.x", {"trial": "indep"})} and bare == 1
    out = trials_eff.deflate({"run": run_book}, "clustered", others=(others, bare))
    assert out["n_eff"] == 1 and out["n_eff_register"] == 3        # run+clone are one family, indep, no_series
    assert out["per_trial"]["run"]["dsr"] == out["per_trial"]["run"]["dsr_register"]
    first = TR.summary()["max_id"]
    TR.record("hedge", [_row("later", daily=_series(rng, days))])
    monkeypatch.setenv(TR.ASOF_ENV, str(first))                     # a pinned rerun ignores later trials
    assert len(TR.others()[0]) == 2


def test_readers_on_the_guardian_or_mcp_never_crash(reg):
    assert TR.count() == 0 and TR.count("strategy.x") == 0          # no file
    reg.parent.mkdir(parents=True)
    reg.write_bytes(b"not a database" * 100)
    assert TR.count() == 0                                           # unreadable: 0, not an error
    with pytest.raises(sqlite3.DatabaseError):
        TR.summary()                                                 # a study reading N must not run on a broken one


def test_cli_migrates_and_summarises(reg, tmp_path, capsys):
    j = tmp_path / "shortbot.db"
    _journal(j)
    assert TR.main(["migrate", "--journal", str(j)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["migrated"]["added"] == 5 and out["summary"]["specs"] == 3 and out["register"] == str(reg)

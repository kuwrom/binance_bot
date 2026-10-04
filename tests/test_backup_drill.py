"""The backup drill (#46, backup_drill.py): the newest local copy restored to a temp dir (never beside the journal)
through backup.restore's verification, then compared with the live journal; one drill_result per run, kv
backup_drill_last_ok on a pass."""

import gzip
import json
from datetime import timedelta
from pathlib import Path

import pytest

from shortbot import backup as B
from shortbot import backup_drill as BD
from shortbot import drill_log
from shortbot.commands import build_parser
from shortbot.journal import Journal
from tests.test_backup import NOW, _cfg, _desk, env  # noqa: F401 - env: the autouse fixture, imported to apply here

LATER = NOW + timedelta(hours=2)


def backed_up(tmp_path: Path) -> tuple[Path, Path]:
    db = _desk(tmp_path, n=20)
    out = B.run([db], _cfg(tmp_path), now=NOW, alert=lambda *a: None)
    assert out["ok"], out
    return db, tmp_path / "backups"


def results(db: Path) -> dict:
    j = Journal(db)
    try:
        return drill_log.latest_results(j)
    finally:
        j.db.close()


def kv(db: Path, k: str):
    j = Journal(db)
    try:
        return j.get(k)
    finally:
        j.db.close()


def test_a_fresh_copy_restores_and_passes_and_the_live_journal_may_have_grown(tmp_path):
    db, d = backed_up(tmp_path)
    j = Journal(db)
    j.create_trade("BBB/USDT:USDT", "pump_fade", "t", 1.1, 2)   # written after the backup
    j.log_decision("skip", "later", None)
    j.db.execute("DELETE FROM kv")                              # pruned markers: kv may shrink
    j.db.commit()
    j.db.close()
    res = BD.run(db, d, now=LATER)
    assert res["verdict"] == "pass" and res["ok"], res["checks"]
    assert res["copy"].startswith("shortbot-") and res["age_h"] == 2.0 and res["temp_removed"]
    assert set(res["checks"]) == {"restore", "tables_le_live", "newest_trade_live", "fresh"}
    assert "manifest" in res["checks"]["restore"]["detail"]
    last = json.loads(kv(db, BD.KV_KEY))
    assert last["copy"] == res["copy"] and last["age_h"] == 2.0
    got = results(db)["backup"]
    assert got["verdict"] == "pass" and got["code_version"] and got["mode"] == "demo"
    assert not list((tmp_path / "data").glob("*restored*"))   # never restored beside the journal


def test_a_copy_older_than_36_h_fails_and_sets_no_kv(tmp_path):
    db, d = backed_up(tmp_path)
    res = BD.run(db, d, now=NOW + timedelta(hours=40))
    assert res["verdict"] == "fail" and not res["checks"]["fresh"]["ok"] and kv(db, BD.KV_KEY) is None


def test_rows_the_live_journal_lost_fail_the_drill(tmp_path):
    db, d = backed_up(tmp_path)
    j = Journal(db)
    j.db.execute("DELETE FROM alerts")   # alerts never shrink: a restore that shows more is a finding
    j.db.commit()
    j.db.close()
    res = BD.run(db, d, now=LATER)
    assert res["verdict"] == "fail"
    assert res["checks"]["tables_le_live"]["detail"] == {"alerts": {"copy": 1, "live": 0}}


def test_a_live_journal_whose_newest_trade_differs_fails(tmp_path):
    db, d = backed_up(tmp_path)
    j = Journal(db)
    j.update_trade(1, opened_at="2020-01-01T00:00:00+00:00")   # not the trade the copy holds under that id
    j.db.close()
    res = BD.run(db, d, now=LATER)
    assert not res["checks"]["newest_trade_live"]["ok"] and res["verdict"] == "fail"


def test_a_corrupt_copy_fails_the_restore_check_and_is_journalled(tmp_path):
    db, d = backed_up(tmp_path)
    (copy,) = d.glob("shortbot-*.db.gz")
    copy.write_bytes(gzip.compress(b"not the journal"))
    res = BD.run(db, d, now=LATER)
    assert res["verdict"] == "fail" and "sha256" in res["checks"]["restore"]["detail"] and res["temp_removed"]
    assert results(db)["backup"]["verdict"] == "fail"


def test_no_copy_at_all_fails(tmp_path):
    db = _desk(tmp_path, n=5)
    res = BD.run(db, tmp_path / "empty", now=LATER)
    assert res["verdict"] == "fail" and res["copy"] is None and "no shortbot-" in res["checks"]["restore"]["detail"]


def test_the_newest_of_several_copies_is_the_one_restored(tmp_path):
    db, d = backed_up(tmp_path)
    assert B.run([db], _cfg(tmp_path), now=NOW + timedelta(hours=1), alert=lambda *a: None)["ok"]
    res = BD.run(db, d, now=LATER)
    assert res["verdict"] == "pass" and res["age_h"] == 1.0


@pytest.mark.parametrize("ok", [True, False])
def test_the_cli_runs_it_and_exits_1_on_a_failure(tmp_path, monkeypatch, capsys, ok):
    db, d = backed_up(tmp_path)
    monkeypatch.setattr(BD, "MAX_AGE_H", 1e9 if ok else 0.0)
    args = build_parser().parse_args(["backup", "drill", "--dir", str(d)])
    assert B.run_cli(args) == (0 if ok else 1)
    assert json.loads(capsys.readouterr().out)["verdict"] == ("pass" if ok else "fail")

import gzip
import json
import sqlite3
import sys
import threading
import time
import types
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shortbot import backup as B, backup_rotate as R, cli
from shortbot.journal import Journal

NOW = datetime(2026, 10, 1, 3, 17, tzinfo=timezone.utc)      # a Thursday, ISO week 40


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Never the real journal, backup dir, off-box command or Telegram (the main checkout's .env is loaded)."""
    for k, v in {"SHORTBOT_DB": str(tmp_path / "data" / "shortbot.db"), "SHORTBOT_MODE": "demo",
                 "SHORTBOT_BACKUP_DIR": str(tmp_path / "backups"), "SHORTBOT_BACKUP_OFFBOX": "",
                 "SHORTBOT_BACKUP_KEEP_DAILY": "7", "SHORTBOT_BACKUP_KEEP_WEEKLY": "4",
                 "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}.items():
        monkeypatch.setenv(k, v)


def _desk(tmp_path: Path, n: int = 300) -> Path:
    """A WAL journal with rows in the research tables that matter."""
    db = tmp_path / "data" / "shortbot.db"
    j = Journal(db)
    j.create_trade("AAA/USDT:USDT", "pump_fade", "lower high", 2.2, 2, plan={"time_stop_hours": 24})
    j.log_decision("skip", "crowded", "BBB/USDT:USDT")
    j.add_learning("pumps fade", "trades 1-3")
    j.log_alert("stop_near", "AAA near stop", "AAA/USDT:USDT")
    j.log_candidates("t", 1, [{"symbol": f"C{i}/USDT:USDT", "price": 1.0, "score": i,
                               "features": {"rsi": i, "pad": "x" * 200}} for i in range(n)], "neutral")
    j.log_prediction("manual", "AAA/USDT:USDT", 2.0, 2.2, 1.8, 24, "bracket", 0.4)
    j.db.close()
    return db


def _cfg(tmp_path: Path, **kw) -> B.BackupConfig:
    return replace(B.BackupConfig(tmp_path / "backups", 7, 4, ""), **kw)


def _count(db: Path, table: str) -> int:
    c = sqlite3.connect(db, timeout=15)
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


def _manifest(tmp_path: Path, name: str) -> dict:
    return json.loads(R.manifest_of(tmp_path / "backups" / name).read_text())


def _alerts() -> tuple[list[tuple[str, str]], object]:
    sent: list[tuple[str, str]] = []
    return sent, lambda kind, message: sent.append((kind, message))


def test_restore_drill_while_other_connections_write(tmp_path, monkeypatch):
    db = _desk(tmp_path)
    before = B._counts(sqlite3.connect(db))
    late, hold = sqlite3.connect(db, timeout=15), sqlite3.connect(db, timeout=15, isolation_level=None)
    real, calls = B._counts, []

    def counts_then_write(conn):
        rows = real(conn)
        if not calls:       # the source snapshot is taken: commit a row, then hold a write transaction over the copy
            late.execute("INSERT INTO decisions (ts, action, reasoning) VALUES ('t', 'note', 'after the snapshot')")
            late.commit()
            hold.execute("BEGIN IMMEDIATE")
            hold.execute("INSERT INTO alerts (ts, kind, message) VALUES ('t', 'x', 'uncommitted during the copy')")
        calls.append(1)
        return rows
    monkeypatch.setattr(B, "_counts", counts_then_write)
    (tmp_path / "backups").mkdir()
    man = B.make_copy(db, tmp_path / "backups", NOW)
    hold.execute("COMMIT")
    monkeypatch.setattr(B, "_counts", real)
    assert man["rows"] == before and man["integrity_check"] == "ok" and man["name"] == "shortbot-20261001T031700Z.db.gz"
    assert _count(db, "decisions") == before["decisions"] + 1 and _count(db, "alerts") == before["alerts"] + 1
    B._write_manifest(tmp_path / "backups", man)

    res = B.restore(tmp_path / "backups" / man["name"], tmp_path / "drill" / "restored.db")
    assert res["ok"] and res["rows"] == before and res["checked"].startswith("manifest")
    r = Journal(tmp_path / "drill" / "restored.db")
    assert [d["reasoning"] for d in r.decisions()] == ["crowded"]
    assert [a["message"] for a in r.alerts()] == ["AAA near stop"]
    assert r.learnings()[0]["statement"] == "pumps fade" and len(r.candidates()) == 300
    assert r.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_run_while_a_writer_commits_nonstop(tmp_path):
    db = _desk(tmp_path, n=3000)
    n0, stop = _count(db, "decisions"), threading.Event()

    def writer():       # far busier than the guardian; a zero-gap loop would starve the kv write's busy handler
        w = sqlite3.connect(db, timeout=15)
        while not stop.is_set():
            w.execute("INSERT INTO decisions (ts, action, reasoning) VALUES ('t', 'note', 'concurrent')")
            w.commit()
            time.sleep(0.002)
        w.close()
    t = threading.Thread(target=writer)
    t.start()
    try:
        while _count(db, "decisions") < n0 + 20:
            time.sleep(0.01)
        alerts, alert = _alerts()
        res = B.run([db], _cfg(tmp_path), now=NOW, alert=alert)
    finally:
        stop.set()
        t.join()
    assert res["ok"] and not alerts and res["warnings"] == [], res
    copy = res["copies"][0]
    assert n0 + 20 <= copy["rows"]["decisions"] <= _count(db, "decisions") and copy["offbox_ok"] is None
    assert json.loads(Journal(db).get("backup_last_ok")) == {"ts": "2026-10-01T03:17:00+00:00",
                                                            "name": copy["name"], "offbox_ok": None}
    restored = B.restore(tmp_path / "backups" / copy["name"], tmp_path / "r.db")
    assert restored["rows"] == copy["rows"]


def test_tampered_backup_and_corrupt_database_are_refused(tmp_path):
    db = _desk(tmp_path)
    f = tmp_path / "backups" / B.run([db], _cfg(tmp_path), now=NOW)["copies"][0]["name"]
    data = bytearray(f.read_bytes())
    data[len(data) // 2] ^= 0xFF
    f.write_bytes(bytes(data))
    with pytest.raises(B.BackupError, match="sha256"):
        B.restore(f, tmp_path / "r.db")
    R.manifest_of(f).unlink()                       # without its manifest the gzip CRC still catches it
    with pytest.raises(Exception):
        B.restore(f, tmp_path / "r.db")
    assert not (tmp_path / "r.db").exists() and not (tmp_path / "r.db.partial").exists()

    bad = tmp_path / "bad.db"
    c = sqlite3.connect(bad)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    c.executemany("INSERT INTO t (v) VALUES (?)", [("y" * 200,)] * 2000)
    c.commit()
    page = c.execute("PRAGMA page_size").fetchone()[0]
    c.close()
    with open(bad, "r+b") as fh:
        fh.seek(page * 9)
        fh.write(b"\xff" * page)
    assert B.verify(bad, None) is not None


def test_restore_never_overwrites(tmp_path):
    db = _desk(tmp_path)
    f = tmp_path / "backups" / B.run([db], _cfg(tmp_path), now=NOW)["copies"][0]["name"]
    (tmp_path / "x.db").write_text("keep me")
    with pytest.raises(B.BackupError, match="exists"):
        B.restore(f, tmp_path / "x.db")
    assert (tmp_path / "x.db").read_text() == "keep me"
    (tmp_path / "y.db-wal").write_text("stale")
    with pytest.raises(B.BackupError, match="y.db-wal exists"):
        B.restore(f, tmp_path / "y.db")
    assert not (tmp_path / "y.db").exists()


def test_rotation_keeps_newest_per_day_and_iso_week(tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    stamps = [NOW - timedelta(days=i) for i in range(40)] + [NOW - timedelta(hours=2)]   # two copies on Oct 1
    for t in stamps:
        (d / f"shortbot-{t:%Y%m%dT%H%M%SZ}.db.gz").write_bytes(b"x")
        R.manifest_of(d / f"shortbot-{t:%Y%m%dT%H%M%SZ}.db.gz").write_text("{}")
    foreign = ["hist5m-20260801T031700Z.db.gz", "shortbot-pinned.db.gz", "notes.txt", "shortbot-20260801T031700Z.db"]
    for name in foreign:
        (d / name).write_text("keep")
    gone = R.rotate(d, "shortbot", 7, 4)
    kept = sorted(p.name for p in d.glob("shortbot-2*Z.db.gz"))
    # daily: Sep 25 - Oct 1 (the 03:17 copy of Oct 1); weekly: + Sep 20 and Sep 13 (Sep 27 already kept as a day)
    assert [k[9:17] for k in kept] == ["20260913", "20260920"] + [f"202609{d}" for d in range(25, 31)] + ["20261001"]
    assert len(gone) == 41 - 9 and "shortbot-20261001T011700Z.db.gz" in gone
    assert sorted(p.name for p in d.glob("shortbot-2*Z.json")) == [k.replace(".db.gz", ".json") for k in kept]
    assert all((d / name).exists() for name in foreign)
    assert R.keep_set(stamps, 0, 0) == {NOW}


def test_offbox_copies_both_files_and_records_it(tmp_path):
    db = _desk(tmp_path)
    remote = tmp_path / "remote dir"                 # a space: the template is split before {file} is filled in
    remote.mkdir()
    res = B.run([db], _cfg(tmp_path, offbox=f"cp {{file}} '{remote}/{{name}}'"), now=NOW)
    name = res["copies"][0]["name"]
    assert res["ok"] and sorted(p.name for p in remote.iterdir()) == [name, name.replace(".db.gz", ".json")]
    assert _manifest(tmp_path, name)["offbox_ok"] is True
    assert json.loads(Journal(db).get("backup_last_ok"))["offbox_ok"] is True


def test_offbox_failure_keeps_the_verified_copy_and_alerts(tmp_path):
    db = _desk(tmp_path)
    alerts, alert = _alerts()
    res = B.run([db], _cfg(tmp_path, offbox="false {file}"), now=NOW, alert=alert)
    name = res["copies"][0]["name"]
    assert not res["ok"] and "off-box copy failed" in res["errors"][0] and [k for k, _ in alerts] == ["backup_failed"]
    assert (tmp_path / "backups" / name).exists() and _manifest(tmp_path, name)["offbox_ok"] is False
    assert json.loads(Journal(db).get("backup_last_ok"))["offbox_ok"] is False


def test_second_run_is_refused_while_the_lock_is_held(tmp_path):
    db, cfg = _desk(tmp_path), _cfg(tmp_path)
    cfg.dir.mkdir()
    alerts, alert = _alerts()
    with B._lock(cfg.dir):
        res = B.run([db], cfg, now=NOW, alert=alert)
    assert not res["ok"] and "another backup is running" in res["errors"][0] and res["copies"] == [] and alerts


def test_refuses_when_the_disk_would_fill(tmp_path, monkeypatch):
    db, cfg = _desk(tmp_path), _cfg(tmp_path)
    monkeypatch.setattr(B.shutil, "disk_usage", lambda _p: types.SimpleNamespace(free=10 * 2**20))
    res = B.run([db], cfg, now=NOW, alert=lambda *_: None)
    assert not res["ok"] and "MB free" in res["errors"][0]
    assert [p.name for p in cfg.dir.iterdir()] == [B.LOCK_FILE] and Journal(db).get("backup_last_ok") is None


def test_research_dbs_closed_wal_and_rollback_side_by_side(tmp_path):
    hist, life = tmp_path / "data" / "hist.db", tmp_path / "data" / "lifecycle.db"
    hist.parent.mkdir()
    for path, wal in ((hist, True), (life, False)):
        c = sqlite3.connect(path)
        if wal:
            c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE bars (symbol TEXT, ts INTEGER, c REAL, PRIMARY KEY (symbol, ts)) WITHOUT ROWID")
        c.executemany("INSERT INTO bars VALUES (?,?,?)", [("AAAUSDT", i, 1.0) for i in range(500)])
        c.commit()
        c.close()
    assert not Path(f"{hist}-wal").exists()          # nobody has it open: the read-only source must still work
    cfg = _cfg(tmp_path, keep_daily=0, keep_weekly=2)
    (cfg.dir / B.TMP_DIR).mkdir(parents=True)
    (cfg.dir / B.TMP_DIR / "hist.db").write_text("left by a killed run")
    (cfg.dir / ".tmp-not-ours").write_text("someone else's")
    res = B.run([hist, life], cfg, now=NOW)
    assert res["ok"] and [c["name"] for c in res["copies"]] == ["hist-20261001T031700Z.db.gz",
                                                                 "lifecycle-20261001T031700Z.db.gz"]
    assert all(c["rows"] == {"bars": 500} for c in res["copies"])
    assert (cfg.dir / ".tmp-not-ours").exists() and list((cfg.dir / B.TMP_DIR).iterdir()) == []
    assert Journal(tmp_path / "data" / "shortbot.db").get("backup_last_ok") is None   # only the desk DB marks it


def test_copytruncate_keeps_the_appending_writer_at_offset_zero(tmp_path):
    log = tmp_path / "guardian.log"
    w = open(log, "ab", buffering=0)                  # what `nohup ... >> guardian.log` hands the guardian
    for gen in range(4):
        w.write(f"gen{gen} ".encode() * 300)
        assert R.rotate_log(log, max_bytes=1000, keep=2) == "rotated to guardian.log.1.gz"
    w.write(b"after\n")
    w.close()
    assert log.read_bytes() == b"after\n"             # no NUL hole: O_APPEND writes land at the new end
    assert gzip.decompress((tmp_path / "guardian.log.1.gz").read_bytes()).startswith(b"gen3")
    assert gzip.decompress((tmp_path / "guardian.log.2.gz").read_bytes()).startswith(b"gen2")
    assert not (tmp_path / "guardian.log.3.gz").exists() and R.rotate_log(log, max_bytes=1000) == "skipped"
    live = tmp_path / "shortbot.db"
    live.write_bytes(b"SQLite format 3\x00" + b"x" * 5000)
    with pytest.raises(ValueError, match="not a \\*.log"):
        R.rotate_log(live, max_bytes=1000)
    assert live.stat().st_size == 5016


def test_cli_backup_never_builds_the_exchange_app(tmp_path, monkeypatch, capsys):
    _desk(tmp_path)
    monkeypatch.setattr(cli, "get_app", lambda *a, **k: pytest.fail("backup must not build the exchange app"))
    log = tmp_path / "g.log"
    log.write_text("x")
    monkeypatch.setattr(sys, "argv", ["shortbot", "backup", "--rotate-log", str(log)])
    with pytest.raises(SystemExit) as e:
        cli.main()
    out = json.loads(capsys.readouterr().out)
    assert e.value.code == 0 and out["copies"][0]["name"].startswith("shortbot-") and out["logs"] == {str(log): "skipped"}
    monkeypatch.setattr(sys, "argv", ["shortbot", "backup", "restore", "whatever.db.gz"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 1 and "usage" in json.loads(capsys.readouterr().out)["error"]


def test_default_alert_lands_in_the_journal(tmp_path):
    db = _desk(tmp_path)
    B._alert("backup_failed", "disk full")
    assert Journal(db).alerts(1)[0]["kind"] == "backup_failed"


def test_busy_journal_kv_mark_is_retried_then_only_a_warning(tmp_path, monkeypatch):
    db = _desk(tmp_path)
    monkeypatch.setattr(B, "KV_BACKOFF_S", 0)
    real, tries = Journal.set, []

    def locked_twice(self, k, v):
        tries.append(k)
        if len(tries) <= 2:
            raise sqlite3.OperationalError("database is locked")
        real(self, k, v)
    monkeypatch.setattr(Journal, "set", locked_twice)
    alerts, alert = _alerts()
    res = B.run([db], _cfg(tmp_path), now=NOW, alert=alert)
    assert res["ok"] and res["warnings"] == [] and not alerts and len(tries) == 3
    assert json.loads(Journal(db).get("backup_last_ok"))["name"] == res["copies"][0]["name"]

    def always_locked(self, k, v):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(Journal, "set", always_locked)
    res = B.run([db], _cfg(tmp_path), now=NOW + timedelta(minutes=1), alert=alert)
    assert res["ok"] and res["errors"] == [] and len(res["copies"]) == 1 and "not recorded" in res["warnings"][0]
    assert [k for k, _ in alerts] == ["backup_warning"]


def test_rotation_failure_still_lists_the_new_copy(tmp_path, monkeypatch):
    db = _desk(tmp_path)

    def broken(*_a):
        raise PermissionError("read-only folder")
    monkeypatch.setattr(B, "rotate", broken)
    alerts, alert = _alerts()
    res = B.run([db], _cfg(tmp_path), now=NOW, alert=alert)
    name = res["copies"][0]["name"]
    assert not res["ok"] and res["errors"] == [f"rotating shortbot copies failed ({name} is fine): "
                                               "PermissionError: read-only folder"]
    assert (tmp_path / "backups" / name).exists() and [k for k, _ in alerts] == ["backup_failed"]
    assert json.loads(Journal(db).get("backup_last_ok"))["name"] == name


def test_default_backup_includes_the_trial_register_when_it_exists(tmp_path, monkeypatch, capsys):
    from shortbot import trial_register as TR
    _desk(tmp_path)
    monkeypatch.delenv(TR.ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["shortbot", "backup"])
    with pytest.raises(SystemExit) as e:
        cli.main()                                   # no register yet: the journal only, not an error
    assert e.value.code == 0 and [c["name"][:9] for c in json.loads(capsys.readouterr().out)["copies"]] == ["shortbot-"]
    monkeypatch.setenv(TR.ENV, str(tmp_path / "data" / "research" / "trials.db"))
    TR.record("backtest", [{"name": "a", "params": {"trial": "a"}, "n": 3}])
    monkeypatch.delenv(TR.ENV)                       # the backup finds it at its default place
    monkeypatch.setenv("SHORTBOT_BACKUP_DIR", str(tmp_path / "backups2"))     # the same second: a fresh dir
    with pytest.raises(SystemExit) as e:
        cli.main()
    copies = json.loads(capsys.readouterr().out)["copies"]
    assert e.value.code == 0 and sorted(c["name"].split("-")[0] for c in copies) == ["shortbot", "trials"]
    assert next(c for c in copies if c["name"].startswith("trials"))["rows"] == {"trials": 1}

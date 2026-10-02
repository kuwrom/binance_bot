"""Provenance of research outputs (provenance.py): every results file names its code, data, window and config, and
a fixture run is reproducible from that record alone."""

import importlib
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from shortbot import histdata
from shortbot import provenance as P
from shortbot.exchange import to_raw
from tests.test_backtest import T0, _synthetic

H = 3_600_000
NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


def _write_hist(path: Path, coins: list[dict]) -> None:
    """The synthetic market in hist.db's own tables, so histdata.load60 reads it back."""
    db = histdata.connect(path)
    for c in coins:
        raw = to_raw(c["symbol"])
        histdata.put_bars(db, raw, "perp", c["perp"], 0)
        histdata.put_bars(db, raw, "prem", [{"ts": t, "o": v, "h": v, "l": v, "c": v, "qv": 0, "n": 0, "tbq": 0}
                                            for t, v in c["premium"].items()], 0)
        histdata.put_funding(db, raw, c["funding"], 0)
        hourly: dict[int, dict] = {}
        for key, col in (("oi", "oi_val"), ("ls_top", "ls_top"), ("ls_global", "ls_global")):
            for t, v in c.get(key, {}).items():
                hourly.setdefault(t, {})[col] = v
        histdata.put_metrics(db, raw, hourly, 0)
        meta = {"symbol": c["symbol"], "raw": raw, "status": "TRADING", "onboard_ms": c["onboard_ms"],
                "delist_ms": c["delist_ms"], "tags": c["tags"], "spot_mult": 1.0}
        db.execute("INSERT OR REPLACE INTO coins VALUES (?,?,?)", (raw, json.dumps(meta), 1))
    db.commit()
    db.close()


def _grow(path: Path, hours: int = 30) -> None:
    """What a weekly rebuild does to hist.db: newer bars appended in place (the delisted coin stays cut)."""
    db = histdata.connect(path)
    for (raw,) in db.execute("SELECT symbol FROM coins WHERE symbol != 'EEEUSDT'").fetchall():
        ts, c = db.execute("SELECT ts, c FROM bars WHERE symbol=? AND kind='perp' ORDER BY ts DESC", (raw,)).fetchone()
        histdata.put_bars(db, raw, "perp", [{"ts": ts + k * H, "o": c, "h": c * 1.01, "l": c * 0.99, "c": c,
                                             "qv": 5e6, "n": 5, "tbq": 2.5e6} for k in range(1, hours + 1)], 0)
    db.commit()
    db.close()


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          capture_output=True, text=True, check=True).stdout.strip()


def _strip(res: dict) -> dict:
    return {k: v for k, v in res.items() if k != "provenance"}


def test_fingerprint_is_the_datas_identity_not_the_files(tmp_path):
    a, b = tmp_path / "a" / "hist.db", tmp_path / "b" / "hist.db"
    _write_hist(a, _synthetic())
    _write_hist(b, _synthetic())
    fa = P.fingerprint(a)
    bars = fa["tables"]["bars"]
    assert bars["rows"] == 6 * 160 * 2 and bars["symbols"] == 6 and bars["time_col"] == "ts"
    assert (bars["min"], bars["max"]) == (T0, T0 + 159 * H)
    assert fa["tables"]["coins"]["time_col"] == "updated_ms" and len(fa["file_sha256"]) == 64
    assert P.fingerprint(b)["id"] == fa["id"]                       # same content, another file
    db = sqlite3.connect(a)
    db.execute("UPDATE bars SET c = c * 1.01 WHERE symbol='AAAUSDT' AND kind='perp' AND ts=?", (T0 + 5 * H,))
    db.commit()
    db.close()
    fa2 = P.fingerprint(a)
    assert fa2["id"] == fa["id"] and fa2["file_sha256"] != fa["file_sha256"]   # value overwrite: only the bytes
    _grow(a, 1)
    assert P.fingerprint(a)["id"] != fa["id"]
    only = P.fingerprint(a, ["coins", "nope"], file_hash=False)
    assert set(only["tables"]) == {"coins", "nope"} and only["tables"]["nope"] is None
    assert "file_sha256" not in only and only["only"] == ["coins", "nope"]
    assert P.fingerprint(tmp_path / "missing.db") == {"path": str((tmp_path / "missing.db").resolve()),
                                                      "missing": True, "id": None}


def test_git_state_reports_sha_dirty_code_only_and_an_installed_copy(tmp_path):
    repo = tmp_path / "repo"
    pkg = repo / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("X = 1\n")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    g = P.git_state(pkg)
    assert g["sha"] == _git(repo, "rev-parse", "HEAD") and g["sha7"] == g["sha"][:7] and g["dirty"] is False
    (repo / "README.md").write_text("docs only\n")
    assert P.git_state(pkg)["dirty"] is False                        # docs don't change a result
    before = P.code_sha(pkg)
    (pkg / "new.py").write_text("Y = 2\n")
    g = P.git_state(pkg)
    assert g["dirty"] is True and g["dirty_paths"] == ["src/pkg/new.py"] and P.code_sha(pkg) != before
    site = tmp_path / "site" / "pkg"
    shutil.copytree(pkg, site)
    assert P.git_state(site)["sha"] is None and P.git_state(site)["sha7"] == "nogit"


def test_outputs_go_under_the_journals_data_dir_and_never_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTBOT_DB", str(tmp_path / "app" / "data" / "shortbot.db"))
    assert P.data_dir() == (tmp_path / "app" / "data").resolve()
    g = {"sha": "abc1234def", "sha7": "abc1234", "dirty": False}
    p = P.run_dir("bt60", g, NOW)
    assert p == (tmp_path / "app" / "data" / "research" / "2026-10-02-abc1234" / "bt60").resolve()
    p.mkdir(parents=True)
    assert P.run_dir("bt60", g, NOW) == p                            # empty: reuse
    (p / "results.json").write_text("{}")
    assert P.run_dir("bt60", g, NOW).name == "bt60-2"
    assert P.run_dir("bt60", {**g, "dirty": True}, NOW).parent.name == "2026-10-02-abc1234-dirty"
    assert P.rerun_args(["--days60", "--out", "x", "--no-save", "--out=y", "--round3"]) == ["--days60", "--round3"]


def test_backtest_results_carry_sha_fingerprint_config_and_rerun_reproduces(tmp_path, monkeypatch, capsys):
    from shortbot.backtest import run
    monkeypatch.setenv("SHORTBOT_DB", str(tmp_path / "data" / "shortbot.db"))
    hist, a = tmp_path / "data" / "hist.db", tmp_path / "a"
    _write_hist(hist, _synthetic())
    run.main(["--days60", "--db", str(hist), "--out", str(a)])
    res = json.loads((a / "results.json").read_text())
    prov = res["provenance"]
    assert prov == json.loads((a / "provenance.json").read_text())
    head = subprocess.run(["git", "-C", str(P.PKG), "rev-parse", "HEAD"], capture_output=True, text=True)
    if head.returncode == 0:
        assert prov["git"]["sha"] == head.stdout.strip() and prov["git"]["dirty"] in (True, False)
    assert prov["code_sha"] == P.code_sha() and prov["env"]["python"] and "ccxt" in prov["env"]
    assert prov["inputs"]["hist_db"]["id"] == P.fingerprint(hist)["id"]
    cfg = prov["config"]
    assert cfg["command"] == "shortbot.backtest.run" and "--out" not in cfg["argv"] and "--days60" in cfg["argv"]
    assert cfg["args"]["placebo"] == "matched" and (cfg["costs"]["taker_bps"], cfg["stress"]["taker_bps"]) == (5.0, 7.5)
    assert len(cfg["ledger"]) == res["n_trials"] == 36
    assert (prov["window"]["t0_ms"], prov["window"]["t1_ms"]) == (T0, T0 + 159 * H)
    assert prov["window"]["coins"] == 6 and cfg["pin"] == ["--t0", str(T0), "--t1", str(T0 + 159 * H),
                                                          "--trials-asof", "0"]   # the trial register it read (#31)
    traded = [n for n, t in res["trials"].items() if t.get("all", {}).get("n")]
    assert traded and all(n in prov["seeds"]["per_trial"] for n in traded)
    md = (a / "report.md").read_text()
    assert md.rstrip().endswith("Full record: provenance.json._") and prov["git"]["sha7"] in md.splitlines()[-1]
    assert P.verify(prov) == [] and P.main(["verify", str(a)]) == 0

    _grow(hist)                                                      # the weekly rebuild, in place
    assert any(d.startswith("hist_db:") for d in P.verify(prov)) and P.main(["verify", str(a)]) == 1
    run.main(["--days60", "--db", str(hist), "--out", str(tmp_path / "grown")])
    grown = json.loads((tmp_path / "grown" / "results.json").read_text())
    assert grown["provenance"]["window"]["t1_ms"] > prov["window"]["t1_ms"]   # unpinned: a different window

    b = tmp_path / "b"
    env = {**os.environ, "PYTHONHASHSEED": "7", "PYTHONPATH": str(P.PKG.parent)}
    subprocess.run(P.rerun_cmd(prov, out=b), env=env, check=True, capture_output=True, cwd=tmp_path)
    again = json.loads((b / "results.json").read_text())
    assert _strip(again) == _strip(res)                              # pinned window, fresh process: same numbers
    assert (b / "trades.json").read_bytes() == (a / "trades.json").read_bytes()
    assert (b / "report.md").read_text().splitlines()[:-1] == md.splitlines()[:-1]
    assert again["provenance"]["inputs"]["hist_db"]["id"] != prov["inputs"]["hist_db"]["id"]


def test_no_save_prints_only_and_the_default_dir_is_under_research(tmp_path, monkeypatch, capsys):
    from shortbot.backtest import run
    monkeypatch.setenv("SHORTBOT_DB", str(tmp_path / "data" / "shortbot.db"))
    hist = tmp_path / "data" / "hist.db"
    _write_hist(hist, _synthetic())
    run.main(["--days60", "--db", str(hist), "--no-save"])
    research = tmp_path / "data" / "research"           # no run dir; a --no-save run still records its trials (#31)
    assert [q.name for q in research.iterdir()] == ["trials.db"] and "Provenance" not in capsys.readouterr().out
    run.main(["--days60", "--db", str(hist), "--round1"])
    out = list((tmp_path / "data" / "research").glob("*/bt60_r1/results.json"))
    assert len(out) == 1 and json.loads(out[0].read_text())["provenance"]["config"]["args"]["round1"] is True


def test_refresh_keeps_the_previous_builds_fingerprint_next_to_the_new_results(tmp_path, monkeypatch):
    from shortbot import refresh
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SHORTBOT_DB", str(tmp_path / "data" / "shortbot.db"))
    hist = tmp_path / "data" / "hist.db"
    _write_hist(hist, _synthetic()[:2])
    calls = []

    def fake_run(cmd, timeout):
        calls.append(cmd)
        if "shortbot.histdata" in cmd:
            assert cmd[cmd.index("--db") + 1] == str(hist.resolve())
            _grow(hist, 2)
        return {"cmd": " ".join(cmd[1:]), "rc": 0}

    monkeypatch.setattr(refresh, "_run", fake_run)
    refresh.main()
    rep_path, = (tmp_path / "data" / "research").glob("*/refresh/refresh.json")
    rep = json.loads(rep_path.read_text())
    h = rep["hist_db"]
    assert h["changed"] and h["before"]["id"] != h["after"]["id"]
    assert h["after"]["tables"]["bars"]["rows"] == h["before"]["tables"]["bars"]["rows"] + 2 * 2
    assert rep["provenance"]["inputs"]["hist_db"] == h["after"] and rep["provenance"]["config"]["histdata_days"] == 64
    bt = calls[1]
    assert bt[bt.index("--db") + 1] == str(hist.resolve()) and bt[bt.index("--out") + 1] == rep["backtest_dir"]
    assert Path(rep["backtest_dir"]).parent == rep_path.parent
    con = sqlite3.connect(tmp_path / "data" / "shortbot.db")          # the journal SHORTBOT_DB names
    why, = con.execute("SELECT reasoning FROM decisions WHERE action='research_refresh'").fetchone()
    con.close()
    assert str(rep_path) in why
    refresh.main()                                                   # a same-day retry keeps the failed week's report
    assert len(list((tmp_path / "data" / "research").glob("*/refresh*/refresh.json"))) == 2


def test_window_bounds_take_epoch_ms_or_a_utc_date():
    from shortbot.backtest.run import _ms
    assert _ms("1785110400000") == 1785110400000 == _ms("2026-07-27")
    assert _ms("2026-07-28T05:00") == 1785110400000 + 29 * H


def test_hedge_report_names_the_backtest_it_read_as_upstream(tmp_path, monkeypatch, capsys):
    from shortbot.backtest import hedge_report, run
    from shortbot.journal import Journal
    monkeypatch.setenv("SHORTBOT_DB", str(tmp_path / "data" / "shortbot.db"))
    Journal(tmp_path / "data" / "shortbot.db").db.close()            # an empty desk journal (no live trades)
    coins = _synthetic()
    coins.append({**coins[0], "symbol": "ETH/USDT:USDT"})            # the overlay needs ETH as well as BTC
    hist, bt = tmp_path / "data" / "hist.db", tmp_path / "bt"
    _write_hist(hist, coins)
    run.main(["--days60", "--db", str(hist), "--out", str(bt)])
    hedge_report.main(["--trades", str(bt / "trades.json"), "--results", str(bt / "results.json"),
                       "--hist", str(hist)])
    out, = (tmp_path / "data" / "research").glob("*/hedge/hedge.json")
    prov = json.loads(out.read_text())["provenance"]
    up = json.loads((bt / "results.json").read_text())["provenance"]
    assert prov["upstream"] == up and prov["window"] == up["window"]
    assert prov["inputs"]["results"]["sha256"] == P.file_digest(bt / "results.json")["sha256"]
    assert prov["inputs"]["journal_trades"]["tables"]["trades"]["rows"] == 0
    assert "--live" in prov["config"]["argv"] and P.verify(prov) == []


def test_trail5m_records_both_caches(tmp_path, monkeypatch, capsys):
    from shortbot.backtest import hist5m, trail5m
    monkeypatch.setenv("SHORTBOT_DB", str(tmp_path / "data" / "shortbot.db"))
    hist, h5 = tmp_path / "data" / "hist.db", tmp_path / "data" / "hist5m.db"
    _write_hist(hist, _synthetic())
    hist5m.connect(h5).close()                                       # empty 5m cache: nothing covered
    trail5m.main(["--hist", str(hist)])
    out, = (tmp_path / "data" / "research").glob("*/trail5m/trail5m.json")
    prov = json.loads(out.read_text())["provenance"]
    assert prov["inputs"]["hist_db"]["id"] == P.fingerprint(hist)["id"]
    assert prov["inputs"]["hist5m_db"]["path"] == str(h5.resolve()) and "--db" in prov["config"]["argv"]


def test_lifecycle_report_saves_its_tables_with_the_dbs_fingerprint(tmp_path, monkeypatch, capsys):
    from shortbot import lifecycle_report as LR
    monkeypatch.setenv("SHORTBOT_DB", str(tmp_path / "data" / "shortbot.db"))
    db = tmp_path / "data" / "lifecycle.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE klines (symbol TEXT, ts INTEGER)")
    con.execute("INSERT INTO klines VALUES ('XUSDT', 5)")
    con.commit()
    con.close()
    seen = []
    monkeypatch.setattr(LR, "load", lambda path: seen.append(Path(path).resolve()) or [])
    monkeypatch.setattr(LR, "report", lambda coins: "| tables |")
    LR.main([])
    out, = (tmp_path / "data" / "research").glob("*/lifecycle/lifecycle.md")
    assert seen == [db.resolve()] and out.read_text().startswith("| tables |\n\n_Provenance:")
    prov = json.loads((out.parent / "provenance.json").read_text())
    assert prov["inputs"]["lifecycle_db"]["tables"]["klines"] == {"rows": 1, "time_col": "ts", "min": 5, "max": 5,
                                                                  "symbols": 1}
    assert prov["config"]["argv"] == ["--db", str(db.resolve())]


@pytest.mark.parametrize("mod", ["carry_regime", "funding_events"])
def test_daily_studies_save_their_json_with_provenance(mod, tmp_path, monkeypatch, capsys):
    m = importlib.import_module(f"shortbot.backtest.{mod}")
    monkeypatch.setenv("SHORTBOT_DB", str(tmp_path / "data" / "shortbot.db"))
    db = tmp_path / "data" / "daily.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE bars (symbol TEXT, ts INTEGER)")
    con.execute("INSERT INTO bars VALUES ('XUSDT', 7)")
    con.commit()
    con.close()
    monkeypatch.setattr(m, "run", lambda path: {"rows": [], "trades": 0, "db": path})
    m.main([])
    out, = (tmp_path / "data" / "research").glob(f"*/{mod}/{mod}.json")
    d = json.loads(out.read_text())
    assert d["db"] == str(db.resolve()) and (out.parent / "provenance.json").exists()
    assert d["provenance"]["inputs"]["daily_db"]["tables"]["bars"]["rows"] == 1
    assert d["provenance"]["config"] == {"command": f"shortbot.backtest.{mod}", "argv": ["--db", str(db.resolve())]}
    m.main(["--out", str(tmp_path / "x.json")])                      # an explicit file still carries it
    x = json.loads((tmp_path / "x.json").read_text())["provenance"]
    assert x["config"]["argv"] == ["--db", str(db.resolve())] and x["inputs"] == d["provenance"]["inputs"]

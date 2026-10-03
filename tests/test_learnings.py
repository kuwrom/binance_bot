"""Learning kind, sample size and review date (#28): a kind is required on new learnings, statistical claims carry
n / days / a sample link, old rows get inferred values (marked '?', never written back), and the brief and
research_report flag what to re-check. Flags only: nothing blocks a learning or an entry."""

import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

from shortbot import learnings_view as lv
from shortbot import cmd_research
from shortbot.cli_brief import brief
from shortbot.journal import Journal
from shortbot.research import report
from tests.test_brief import app

TODAY = date(2026, 10, 2)
FIELDS = ("kind", "n", "days", "sample", "review_by")
ZRO_EVIDENCE = ("ZRO #8 2026-09-29: short 1.648 after the failed 1.65 sweep; the 13:00 1h closed 1.6687 (inside "
                "the p90 fake-out 2.4-4%); consistent with learnings #4/#6 and the tape's accepted_breakout = 2 "
                "closes.")


def _trade(j, tid, symbol, opened):
    j.db.execute("INSERT INTO trades (id, symbol, opened_at) VALUES (?,?,?)", (tid, symbol, opened))
    j.db.commit()


def _age(j, lid, created, updated=None):
    j.db.execute("UPDATE learnings SET created_at=?, updated_at=? WHERE id=?", (created, updated or created, lid))
    j.db.commit()


def _legacy(j, statement, evidence, status="hypothesis", confidence="low", created="2026-09-29 14:05:16"):
    """A learning as recorded before #28: no kind, no sample fields."""
    lid = j.add_learning(statement, evidence, None, status, confidence)
    _age(j, lid, created)
    return lid


def _audit(j, lid, today=TODAY):
    return next(r["audit"] for r in lv.annotate(j, j.learnings(), today) if r["id"] == lid)


def _section(text, title=lv.SECTION):
    lines = text.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith(title))
    return [ln for ln in lines[i + 1:next((k for k in range(i + 1, len(lines)) if lines[k].startswith("###")),
                                         len(lines))]]


def test_old_database_gains_the_fields_and_old_rows_read_back_empty(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE learnings (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
                " setup TEXT, statement TEXT NOT NULL, evidence TEXT, status TEXT NOT NULL, confidence TEXT,"
                " provenance TEXT)")
    con.execute("INSERT INTO learnings VALUES (1, '2026-09-28 07:15:10', '2026-09-28 07:15:10', NULL, 'stops',"
                " 'fixed, test_x', 'adopted', 'high', NULL)")
    con.commit()
    con.close()
    row = Journal(db).learnings()[0]
    assert row["statement"] == "stops" and {k: row[k] for k in FIELDS} == dict.fromkeys(FIELDS)


async def test_add_learning_tool_requires_a_kind_and_stores_the_sample(monkeypatch):
    j = Journal(":memory:")
    monkeypatch.setattr(cmd_research, "get_app", lambda: SimpleNamespace(journal=j))
    with pytest.raises(Exception, match="kind"):
        await cmd_research.add_learning("x", "e", ["trade:1"])
    with pytest.raises(Exception, match="kind"):
        await cmd_research.add_learning("x", "e", ["trade:1"], "anecdote")
    await cmd_research.add_learning("pumps fade in Asia", "research_report by_session", ["research_report"],
                                    "statistical", n=41, days=12, sample="research_report:2026-10-02",
                                    review_by="2026-10-20")
    row = j.learnings()[0]
    assert tuple(row[k] for k in FIELDS) == ("statistical", 41, 12, "research_report:2026-10-02", "2026-10-20")
    lid = row["id"]
    await cmd_research.update_learning(lid, n=55, sample="registry:lib.asia_fade")
    row = j.learnings()[0]
    assert (row["n"], row["days"], row["sample"]) == (55, 12, "registry:lib.asia_fade")
    seen = (await cmd_research.journal())["learnings"][0]
    assert seen["audit"]["kind"] == "statistical" and seen["audit"]["inferred"] == []


async def test_cli_learn_takes_the_same_fields(monkeypatch):
    from shortbot import cli, cmd_research
    from shortbot.commands import build_parser
    j = Journal(":memory:")
    monkeypatch.setattr(cmd_research, "get_app", lambda *a, **k: SimpleNamespace(journal=j))

    async def no_close():
        return None
    monkeypatch.setattr(cli, "close_app", no_close)
    learn = ["learn", "pumps fade in Asia", "--evidence", "research_report by_session", "--provenance",
             "research_report"]
    args = build_parser().parse_args(learn + ["--kind", "statistical", "--n", "41", "--days", "12", "--sample",
                                              "research_report:2026-10-02", "--review-by", "2026-10-20"])
    assert (await cli._run(args))["ok"]
    row = j.learnings()[0]
    assert tuple(row[k] for k in FIELDS) == ("statistical", 41, 12, "research_report:2026-10-02", "2026-10-20")
    with pytest.raises(SystemExit):
        build_parser().parse_args(learn + ["--kind", "vibes"])
    with pytest.raises(ValueError, match="review_by"):
        await cli._run(build_parser().parse_args(learn + ["--kind", "process", "--review-by", "next week"]))
    with pytest.raises(SystemExit):   # required, as add_learning requires it (the CLI used to store kind NULL)
        build_parser().parse_args(["learn", "no kind given", "--evidence", "e", "--provenance", "trade:1"])


@pytest.mark.parametrize("bad", [{"kind": "vibes"}, {"n": -1}, {"n": "many"}, {"days": 1.5}, {"days": -2},
                                 {"review_by": "next week"}, {"sample": "x" * 300}])
def test_bad_fields_are_refused(bad):
    j = Journal(":memory:")
    with pytest.raises(ValueError):
        j.add_learning("x", "e", **{"kind": "statistical", **bad})
    lid = j.add_learning("x", "e", kind="statistical")
    with pytest.raises(ValueError):
        j.update_learning(lid, **bad)


def test_old_rule_from_one_trade_shows_as_statistical_n1_and_keeps_its_status():
    """Learning #11's shape: a rule adopted from a single trade (ZRO #8)."""
    j = Journal(":memory:")
    _trade(j, 8, "ZRO/USDT:USDT", "2026-09-29T12:40:00+00:00")
    lid = _legacy(j, "Invalidations must mean ACCEPTANCE, not one close", ZRO_EVIDENCE, "adopted", "medium")
    a = _audit(j, lid)
    assert (a["kind"], a["n"], a["days"]) == ("statistical", 1, 1) and set(a["inferred"]) == {"kind", "n", "days"}
    assert {"anecdote", "pre_episodes"} <= set(a["flags"])
    row = j.learnings()[0]
    assert row["status"] == "adopted" and all(row[k] is None for k in FIELDS)   # nothing written back


def test_inference_rules_for_old_rows():
    j = Journal(":memory:")
    _trade(j, 5, "ONDO/USDT:USDT", "2026-09-28T04:43:00+00:00")
    eng = _legacy(j, "stop swaps re-place", "ONDO #5: fixed in stops.py, tests test_move_stop_drift_*", "adopted",
                  "high")
    study = _legacy(j, "rounds run further", "sweeps report: round n=1513, control n=1131; ONDO #5", "supported")
    wrong = _legacy(j, "a hunch", "QNT #5 looked weak")          # trade 5 is ONDO: not a citation
    assert _audit(j, eng)["kind"] == "engineering" and _audit(j, eng)["flags"] == []
    a = _audit(j, study)
    assert a["kind"] == "statistical" and a["n"] is None and "n_unknown" in a["flags"]
    assert _audit(j, wrong)["n"] is None


def test_values_set_by_hand_win_and_clear_the_pre_episode_flag():
    j = Journal(":memory:")
    oi = _legacy(j, "OI floor", "774 candidates logged since 09-27", created="2026-09-28 09:55:29")
    lit = _legacy(j, "pattern registry governance", "literature review", created="2026-09-29 16:26:25")
    post = _legacy(j, "pump fades decayed", "539 scan episodes", created="2026-10-01 08:31:08")
    assert "pre_episodes" in _audit(j, oi)["flags"] and "pre_episodes" not in _audit(j, post)["flags"]
    j.update_learning(oi, n=212, days=5, sample="research_report:2026-10-02")
    j.update_learning(lit, kind="process")
    a = _audit(j, oi)
    assert (a["n"], a["days"], a["inferred"]) == (212, 5, ["kind"]) and "pre_episodes" not in a["flags"]
    assert _audit(j, lit)["kind"] == "process" and not {"pre_episodes", "n_unknown"} & set(_audit(j, lit)["flags"])


def test_review_dates_default_by_status_and_explicit_dates_win():
    j = Journal(":memory:")
    hyp = _legacy(j, "h", "e", created="2026-09-10 00:00:00")
    sup = _legacy(j, "s", "e", "supported", "medium", created="2026-09-10 00:00:00")
    adopted = _legacy(j, "a", "e", "adopted", "high", created="2026-01-01 00:00:00")
    pinned = j.add_learning("p", "e", kind="statistical", review_by="2026-12-01")
    _age(j, pinned, "2026-01-01 00:00:00")
    assert _audit(j, hyp)["review_by"] == "2026-09-24" and "review_due" in _audit(j, hyp)["flags"]
    assert _audit(j, sup)["review_by"] == "2026-10-10" and "review_due" not in _audit(j, sup)["flags"]
    assert _audit(j, adopted)["review_by"] is None and "review_due" not in _audit(j, adopted)["flags"]
    assert _audit(j, pinned)["review_by"] == "2026-12-01" and "review_due" not in _audit(j, pinned)["flags"]


async def test_a_hypothesis_past_its_review_date_is_resurfaced_in_the_brief():
    a = app()
    j = a.journal
    due = j.add_learning("funding flips predict squeezes", "3 trades", kind="statistical", n=3, review_by="2026-09-30")
    fresh = j.add_learning("later idea", "1 trade", kind="statistical", review_by="2999-01-01")
    stale = j.add_learning("old hunch", "2 trades", kind="statistical")
    _age(j, stale, "2026-08-01 00:00:00")                         # default: last update + 14 days
    sec = _section(await brief(a))
    due_lines = [ln for ln in sec if ln.startswith("- review due")]
    assert [ln.split(": #")[1].split()[0] for ln in due_lines] == [str(stale), str(due)]   # oldest date first
    assert "old hunch" in due_lines[0] and "[hypothesis/low stat n=?]" in due_lines[0]
    assert not any(f"#{fresh} " in ln for ln in sec)


async def test_many_due_learnings_fold_into_one_line():
    a = app()
    ids = [a.journal.add_learning(f"idea {i}", "e", kind="statistical", review_by="2026-09-01") for i in range(8)]
    due_lines = [ln for ln in _section(await brief(a)) if ln.startswith("- review due")]
    assert len(due_lines) == lv.DUE_SHOWN + 1 and all(f"#{i}" in due_lines[-1] for i in ids[lv.DUE_SHOWN:])


async def test_brief_tags_learnings_and_lists_what_to_recheck():
    a = app()
    j = a.journal
    _trade(j, 8, "ZRO/USDT:USDT", "2026-09-29T12:40:00+00:00")
    old = _legacy(j, "acceptance, not one close", ZRO_EVIDENCE, "adopted", "medium")
    new = j.add_learning("drawdown ladder", "bootstrap", kind="statistical", n=120, days=40,
                         sample="data/research/2026-10-01-abc1234/drawdown")
    eng = j.add_learning("stops re-place", "tests/test_stops.py", kind="engineering")
    text = await brief(a)
    by_id = {ln.split()[1]: ln for ln in text.splitlines() if ln.startswith("- #")}
    assert "stat? n=1? 1d?" in by_id[f"#{old}"] and "stat n=120 40d" in by_id[f"#{new}"]
    assert "eng]" in by_id[f"#{eng}"]
    sec = _section(text)
    assert not any(ln.startswith("- #") for ln in sec)
    assert f"#{old} (n=1?)" in next(ln for ln in sec if "n < 30" in ln)
    assert f"#{old}" in next(ln for ln in sec if "before episode counting" in ln)
    assert "1 learning" in next(ln for ln in sec if "no kind" in ln)
    assert not any(f"#{new}" in ln or f"#{eng}" in ln for ln in sec)


async def test_brief_has_no_recheck_section_when_nothing_is_due():
    a = app()
    a.journal.add_learning("drawdown ladder", "bootstrap", kind="statistical", n=120, days=40)
    assert not any(ln.startswith(lv.SECTION) for ln in (await brief(a)).splitlines())


def test_research_report_lists_statistical_learnings_with_n_and_days():
    j = Journal(":memory:")
    s = j.add_learning("x", "e", kind="statistical", n=40, days=12, sample="research_report:2026-10-02")
    j.add_learning("y", "tests/test_x.py", kind="engineering")
    _trade(j, 8, "ZRO/USDT:USDT", "2026-09-29T12:40:00+00:00")
    old = _legacy(j, "acceptance", ZRO_EVIDENCE, "adopted", "medium")
    out = report(j)["learnings"]
    rows = {r["id"]: r for r in out["statistical"]}
    assert set(rows) == {s, old} and out["statistical"][0]["id"] == old         # lowest n first
    assert (rows[s]["n"], rows[s]["days"], rows[s]["sample"]) == (40, 12, "research_report:2026-10-02")
    assert (rows[old]["n"], rows[old]["days"], rows[old]["status"]) == (1, 1, "adopted")
    assert out["by_kind"] == {"engineering": 1, "statistical": 2, "process": 0} and out["kind_inferred"] == 1
    assert old in out["anecdote"] and old in out["pre_episodes"]
    j.log_candidates("2026-10-01T00:00:00+00:00", 1, [{"symbol": "X/USDT:USDT", "price": 1.0}], None)
    j.label_candidate(1, {"short_ret_24h": 1.0, "max_adverse_24h": 2.0})
    assert {r["id"] for r in report(j)["learnings"]["statistical"]} == {s, old}

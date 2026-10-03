"""Outcome scores and curation (learnings_view.py): a learning scores by the R of the closed trades whose plan cited
it (never by votes), the rulebook gate `settled`, the harmful / expired / missing_rule flags, and the compact index
the journal tool returns."""

from datetime import date

import pytest

from shortbot import learnings_view as lv
from shortbot.journal import Journal
from shortbot.research import report

TODAY = date(2026, 10, 3)


def trade(j, cited, r=None, mode=None):
    """A short with 1R = 10 USDT citing `cited`; closed at R = r (None: still open)."""
    tid = j.create_trade("ZRO/USDT:USDT", "pump_fade", "t", 1.1, 2, plan={"uses_learnings": cited})
    j.activate_trade(tid, 1.0, 100.0, 100.0)
    if r is not None:
        j.close_trade(tid, 1.0, r * 10, "test")
    if mode:
        j.update_trade(tid, mode=mode)
    return tid


def audit(j, lid, today=TODAY):
    return next(r for r in lv.annotate(j, j.learnings(active_only=False), today) if r["id"] == lid)


def test_scores_count_closed_trades_by_r_in_every_mode_once_per_trade():
    j = Journal(":memory:")
    a = j.add_learning("a", "e", kind="process")
    b = j.add_learning("b", "e", kind="process")
    win = trade(j, [a], 1.0)
    trade(j, [a], None)                                       # still open: not an outcome yet
    lose = trade(j, [b, b], -1.0)                             # a hand-written plan citing twice counts once
    live = trade(j, [b], -0.5, mode="live")                   # the claim is about the market, not the account
    flat = trade(j, [a, b], 0.03)                             # a scratch: cited, neither helpful nor harmful
    s = lv.scores(j)
    assert s[a] == {"helpful": 1, "harmful": 0, "cited_by": [win, flat]}
    assert s[b] == {"helpful": 0, "harmful": 2, "cited_by": [lose, live, flat]}
    x = audit(j, b)["audit"]
    assert (x["helpful"], x["harmful"], "harmful" in x["flags"]) == (0, 2, True)
    assert "harmful" not in audit(j, a)["audit"]["flags"]


def _row(j, status="supported", kind="statistical", n=40, **kw):
    return j.add_learning("pumps fade after the failed sweep", "e", status=status, kind=kind, n=n, **kw)


@pytest.mark.parametrize("make, settled", [
    (lambda j: _row(j, "adopted", n=1), True),               # adopted stands on its own, even an anecdote
    (lambda j: _row(j), True),                                # supported on a real sample
    (lambda j: _row(j, n=5), False),                          # anecdote
    (lambda j: _row(j, n=None), False),                       # n_unknown
    (lambda j: _row(j, kind="process", n=None), True),
    (lambda j: _row(j, "adopted", kind="engineering"), False),
    (lambda j: _row(j, "hypothesis"), False),
    (lambda j: _row(j, "rejected"), False),
    (lambda j: _row(j, "adopted", valid_until="2026-10-02"), False),   # expired
    (lambda j: _row(j, "adopted", valid_until="2026-10-03"), True),    # current through that day
])
def test_the_rulebook_gate(make, settled):
    j = Journal(":memory:")
    assert lv.settled(audit(j, make(j))) is settled


def test_harmful_and_superseded_rows_leave_the_rulebook():
    j = Journal(":memory:")
    sup, adopted, old = _row(j), _row(j, "adopted"), _row(j, "adopted", valid_until="2026-01-01")
    for _ in range(2):
        trade(j, [sup, adopted], -1.0)
    assert not lv.settled(audit(j, sup)) and lv.settled(audit(j, adopted))   # adopted: the flag asks for a decision
    j.update_learning(old, superseded_by=adopted)
    r = audit(j, old)
    assert r["audit"]["flags"] == [] and not lv.settled(r)    # history: no flags, never in the rulebook


def test_research_report_lists_harmful_expired_and_missing_rule():
    j = Journal(":memory:")
    bad = _row(j, "adopted", rule="fade the failed sweep")
    gone = _row(j, valid_until="2026-01-01")
    bare = _row(j, "adopted")
    for _ in range(2):
        trade(j, [bad], -1.0)
    out = report(j)["learnings"]
    assert out["harmful"] == [bad] and out["expired"] == [gone] and out["missing_rule"] == [bare]
    lines = lv.brief_lines(lv.annotate(j, j.learnings()))
    assert any(f"#{bad} (+0/-2)" in ln for ln in lines) and any(f"#{gone} (2026-01-01)" in ln for ln in lines)
    assert not any(ln.startswith("- #") for ln in lines)


def test_rank_text_and_the_compact_index():
    j = Journal(":memory:")
    hyp = j.add_learning("h " * 200, "long evidence " * 50, kind="statistical", provenance=["coin_news:QNT"])
    sup = _row(j, n=200)
    small = _row(j, n=40)
    ado = _row(j, "adopted", n=3, rule="fade the failed sweep, stop beyond the p90 fake-out")
    trade(j, [small], 2.0)
    rows = lv.annotate(j, j.learnings())
    assert [r["id"] for r in lv.rank(rows)] == [ado, small, sup, hyp]   # adopted, score, n, recency
    idx = {r["id"]: r for r in lv.index(rows)}
    assert idx[ado]["text"] == "fade the failed sweep, stop beyond the p90 fake-out"
    assert len(idx[hyp]["text"]) == lv.TEXT_MAX and "third_party" in idx[hyp]["flags"]
    assert idx[small]["helpful"] == 1 and all("evidence" not in r and "statement" not in r for r in idx.values())
    assert set(idx[sup]) == {"id", "status", "tag", "setup", "text", "helpful", "harmful", "flags"}
    assert len(lv.index(rows, limit=2)) == 2

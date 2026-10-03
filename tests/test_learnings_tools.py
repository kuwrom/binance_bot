"""The learnings tools: `journal` returns a compact index (no evidence), `learnings` returns full rows by query, setup or
id (the CLI parses the same), `add_learning` returns the similar learnings it found first, and read_coin's lessons
see a learning added after the last registry seed."""

from types import SimpleNamespace

import pytest

from shortbot import cli, cmd_research
from shortbot.commands import build_parser
from shortbot.journal import Journal
from shortbot.read import registry_view


@pytest.fixture
def j(monkeypatch):
    j = Journal(":memory:")
    for mod in (cmd_research, cli):
        monkeypatch.setattr(mod, "get_app", lambda *a, **k: SimpleNamespace(journal=j))

    async def no_close():
        return None
    monkeypatch.setattr(cli, "close_app", no_close)
    return j


def seed(j):
    a = j.add_learning("pump fades need a failed sweep", "ZRO #8 " * 50, "pump_fade", "adopted", kind="process",
                       rule="fade a pump only after a failed sweep of its high")
    b = j.add_learning("breakdown retests fail", "3 trades", "breakdown", kind="statistical", n=3)
    c = j.add_learning("funding flips precede squeezes", "e", None, "rejected", kind="statistical")
    return a, b, c


async def test_the_journal_returns_a_compact_index(j, monkeypatch):
    a, b, c = seed(j)
    out = await cmd_research.journal()
    idx = {r["id"]: r for r in out["learnings"]}
    assert set(idx) == {a, b} and out["learnings_more"] == 0       # current only: the rejected one is searched
    assert idx[a]["text"] == "fade a pump only after a failed sweep of its high"
    assert all("evidence" not in r and "statement" not in r and "audit" not in r for r in idx.values())
    monkeypatch.setattr(cmd_research, "INDEX_MAX", 1)
    out = await cmd_research.journal()
    assert [r["id"] for r in out["learnings"]] == [a] and out["learnings_more"] == 1


async def test_the_learnings_tool_by_query_setup_and_id(j):
    a, b, c = seed(j)
    hits = (await cmd_research.learnings("sweeps"))["learnings"]
    assert [r["id"] for r in hits] == [a] and "ZRO #8" in hits[0]["evidence"] and "audit" in hits[0]
    assert [r["id"] for r in (await cmd_research.learnings(setup="breakdown"))["learnings"]] == [b]
    assert (await cmd_research.learnings("squeezes"))["learnings"] == []          # rejected: not current
    assert [r["id"] for r in (await cmd_research.learnings("squeezes", include_inactive=True))["learnings"]] == [c]
    out = await cmd_research.learnings(ids=[c, 99, a])
    assert [r["id"] for r in out["learnings"]] == [c, a] and out["missing"] == [99]   # any state, in the order asked
    listed = await cmd_research.learnings(limit=1)
    assert [r["id"] for r in listed["learnings"]] == [a] and listed["more"] == 1      # ranked: adopted first
    args = build_parser().parse_args(["learnings", "retests", "--setup", "breakdown", "--limit", "3"])
    assert [r["id"] for r in (await cli._run(args))["learnings"]] == [b]
    args = build_parser().parse_args(["learnings", "--id", str(b), "--id", str(c), "--include-inactive"])
    assert [r["id"] for r in (await cli._run(args))["learnings"]] == [b, c]


async def test_add_learning_returns_similar_learnings_before_its_own(j):
    a, _, _ = seed(j)
    out = await cmd_research.add_learning("pumps fade after failed sweeps", "trade:9", ["trade:9"], "statistical",
                                          rule="short the failed sweep", valid_until="2026-12-31")
    similar = [s["id"] for s in out["similar"]]
    assert out["ok"] and similar[0] == a and out["learning_id"] not in similar   # searched before the insert
    row = j.learnings(ids=[out["learning_id"]])[0]
    assert (row["rule"], row["valid_until"]) == ("short the failed sweep", "2026-12-31")
    args = build_parser().parse_args(["update-learning", str(a), "--superseded-by", str(out["learning_id"]),
                                      "--rule", "", "--valid-until", "2027-01-01", "--setup", ""])
    assert (await cli._run(args))["ok"]
    row = j.learnings(ids=[a], active_only=False)[0]
    assert (row["superseded_by"], row["rule"], row["valid_until"], row["setup"]) == (
        out["learning_id"], None, "2027-01-01", None)


def test_read_coin_lessons_include_a_learning_added_after_the_last_seed(j):
    lid = j.add_learning("ZRO fakeouts run 2-5% past the level", "ZRO #8", kind="statistical")
    reg = registry_view(j, "post_pump", "failed_sweep", [], ["pump_fade"], "ZRO/USDT:USDT")
    assert {"kind": "learning", "ref": f"#{lid}", "status": "hypothesis",
            "snippet": "ZRO fakeouts run 2-5% past the level"} in reg["lessons"]

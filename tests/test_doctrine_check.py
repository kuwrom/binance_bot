"""doctrine_check.py: the brief names every learning DOCTRINE.md or a skill cites that the journal doesn't hold as
settled (missing, rejected, superseded, expired, a hypothesis, or supported without a real sample)."""

from datetime import UTC, datetime, timedelta

from shortbot import doctrine_check as dc
from shortbot import learnings_brief as lb
from shortbot import learnings_view as lv
from shortbot.journal import Journal


def files(tmp_path, doctrine, skill=""):
    d = tmp_path / "DOCTRINE.md"
    d.write_text(doctrine)
    (tmp_path / "skills" / "pump-fade").mkdir(parents=True)
    (tmp_path / "skills" / "pump-fade" / "SKILL.md").write_text(skill)
    return d, tmp_path / "skills"


def test_cited_reads_bracketed_ids_in_the_doctrine_and_learning_ids_in_skills(tmp_path):
    d, s = files(tmp_path, "Unlocks (44): 2%+ of supply (16 trades: promising). Listings (15, 36). Risk (0.75 when "
                           "cautious). Open interest (OI) every 8h (or 1h).",
                 "Split entries (learning #29); tells last a day (learnings #79, #80). ZRO #8 and drill #46 are not.")
    assert dc.cited(d, s) == {44: "DOCTRINE.md", 15: "DOCTRINE.md", 36: "DOCTRINE.md", 29: "pump-fade skill",
                              79: "pump-fade skill", 80: "pump-fade skill"}
    assert dc.cited(tmp_path / "missing.md", tmp_path / "no-skills") == {}


def test_the_line_names_each_citation_that_is_not_settled_and_why():
    j = Journal(":memory:")
    ok = j.add_learning("settled", "e", status="supported", kind="statistical", n=200)
    proc = j.add_learning("adopted process", "e", status="adopted", kind="process")
    eng = j.add_learning("an engineering fact", "tests/test_x.py", status="supported", kind="engineering")
    hyp = j.add_learning("an idea", "e", kind="process")
    thin = j.add_learning("three trades", "e", status="supported", kind="statistical", n=3)
    bare = j.add_learning("no sample given", "e", status="supported", kind="statistical")
    gone = j.add_learning("refuted", "e", kind="process")
    j.update_learning(gone, status="rejected")
    old = j.add_learning("a tell from one day", "e", status="supported", kind="statistical", n=200,
                         valid_until=(datetime.now(UTC).date() - timedelta(days=1)).isoformat())
    rows = lv.annotate(j, j.learnings(active_only=False))
    cites = {i: "DOCTRINE.md" for i in (ok, proc, eng, hyp, thin, bare, gone, old)} | {999: "pump-fade skill"}
    line = dc.brief_line(rows, cites)
    assert f"#{ok} " not in line and f"#{proc} " not in line and f"#{eng} " not in line
    for i, why in ((hyp, "hypothesis"), (thin, "n < 30"), (bare, "sample not stated"), (gone, "rejected"),
                   (old, "expired")):
        assert f"#{i} ({why}; DOCTRINE.md)" in line
    assert "#999 (no such learning; pump-fade skill)" in line
    assert dc.brief_line(rows, {ok: "DOCTRINE.md"}) is None and dc.brief_line(rows, {}) is None


def test_the_brief_shows_it_under_re_check_even_when_nothing_else_is_flagged(tmp_path, monkeypatch):
    j = Journal(":memory:")
    ok = j.add_learning("settled", "e", status="adopted", kind="process")
    gone = j.add_learning("refuted", "e", kind="process")
    j.update_learning(gone, status="rejected")
    d, s = files(tmp_path, f"A rule ({ok}). A dead rule ({gone}).")
    monkeypatch.setattr(dc, "DOCTRINE", d)
    monkeypatch.setattr(dc, "SKILLS", s)
    lines = lb.section(j, [])
    at = next(i for i, ln in enumerate(lines) if ln.startswith(lv.SECTION))
    assert f"#{gone} (rejected; DOCTRINE.md)" in lines[at + 1] and f"#{ok} " not in lines[at + 1]
    d.write_text(f"A rule ({ok}).")
    assert not any(ln.startswith(lv.SECTION) for ln in lb.section(j, []))


def test_every_learning_the_repo_cites_is_a_number_the_check_can_read():
    cites = dc.cited()   # the real DOCTRINE.md and skills: the citation format still parses
    assert cites and all(src == "DOCTRINE.md" or src.endswith(" skill") for src in cites.values())


def test_the_rulebook_lists_a_rule_the_doctrine_states_by_id_only(tmp_path, monkeypatch):
    j = Journal(":memory:")
    a = j.add_learning("stated in the doctrine", "e", status="adopted", kind="process", rule="rule A")
    j.add_learning("only in the journal", "e", status="adopted", kind="process", rule="rule B")
    c = j.add_learning("cited by a skill only", "e", status="adopted", kind="process", rule="rule C")
    d, s = files(tmp_path, f"A rule ({a}).", f"A check (learning #{c}).")
    monkeypatch.setattr(dc, "DOCTRINE", d)
    monkeypatch.setattr(dc, "SKILLS", s)
    lines = lb.section(j, [])
    assert not any("rule A" in ln for ln in lines) and sum("rule B" in ln or "rule C" in ln for ln in lines) == 2
    assert [ln for ln in lines if "stated in docs/DOCTRINE.md" in ln][0].endswith(f": #{a}")
    assert not any("more current learnings" in ln for ln in lines)       # all three are accounted for

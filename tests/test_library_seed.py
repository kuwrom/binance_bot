import json
import sqlite3

import pytest

from shortbot import library_seed as L
from shortbot import registry as R

CARD = """# Long-side strategies

### oi_flush_long — OI flush dip buy
- direction: long
- family: microstructure
- mechanism: forced long liquidations overshoot
- rules: buy after OI -10% in 4h with negative funding; stop below the flush low
- data needed: OI, funding (Binance public)
- evidence: n=40
- grade: Single-source
- fit: Med — needs long support

## Notes
- direction: ignored, not inside a card
"""


def db():
    d = sqlite3.connect(":memory:")
    d.execute("CREATE TABLE learnings (id INTEGER PRIMARY KEY, created_at TEXT, updated_at TEXT, setup TEXT,"
              " statement TEXT, evidence TEXT, status TEXT, confidence TEXT)")
    R.ensure(d)
    return d


def test_parse_reads_cards_and_stops_at_the_next_heading():
    [c] = L.parse(CARD, "docs/strategies/x.md")
    assert c["id"] == "oi_flush_long" and c["name"] == "OI flush dip buy"
    assert c["direction"] == "long" and c["data_needed"].startswith("OI")


def test_parse_rejects_a_card_missing_required_fields():
    with pytest.raises(ValueError, match="missing"):
        L.parse("### bad_card — Bad\n- direction: long\n")
    with pytest.raises(ValueError, match="direction"):
        L.parse(CARD.replace("direction: long", "direction: sideways"))


def test_seed_registers_drafts_once_and_versions_on_rule_change(tmp_path):
    (tmp_path / "a.md").write_text(CARD)
    d = db()
    assert L.seed_library(d, tmp_path) == {"cards": 1, "new": 1}
    assert L.seed_library(d, tmp_path) == {"cards": 1, "new": 0}
    p = R.latest(d, "lib.oi_flush_long")
    assert p["status"] == "draft" and p["level"] == "strategy" and json.loads(p["definition"])["direction"] == "long"
    (tmp_path / "a.md").write_text(CARD.replace("OI -10%", "OI -15%"))
    assert L.seed_library(d, tmp_path)["new"] == 1 and R.latest(d, "lib.oi_flush_long")["version"] == 2


def test_duplicate_ids_across_files_are_refused(tmp_path):
    (tmp_path / "a.md").write_text(CARD)
    (tmp_path / "b.md").write_text(CARD)
    with pytest.raises(ValueError, match="duplicate"):
        L.load(tmp_path)


def test_the_real_library_parses():
    if L.LIB_DIR.exists():
        cards = L.load()
        assert all(c["direction"].split()[0] in L.DIRECTIONS for c in cards)
        assert all(c["fit"].split(" ")[0] in ("High", "Med", "Low") for c in cards)
        assert "| id |" in L.table()

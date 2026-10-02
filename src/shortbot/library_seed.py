"""Strategy library (docs/strategies/*.md) -> pattern registry, as `draft` strategy nodes with no detector yet.

Each card in those files is one strategy we have read about but not built: long, short or market-neutral. A card
becomes `lib.<id>` (level strategy). It moves to shadow only once it has a detector and episodes (docs/REGISTRY.md).
Card format (strict, so the docs stay the single source of truth):

    ### <snake_case_id> — <Name>
    - direction: long | short | both | neutral
    - family: carry | trend | ...
    - mechanism: ...
    - rules: / evidence: / grade: / fit: ...   (any other `- key: value` bullets are kept in the definition)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import registry as R

LIB_DIR = Path(__file__).resolve().parents[2] / "docs" / "strategies"
REQUIRED = ("direction", "family", "mechanism", "rules", "evidence", "grade", "fit")
DIRECTIONS = ("long", "short", "both", "neutral")
RULE_KEYS = ("direction", "family", "rules", "data_needed")   # the registry definition: a change = a new version
_HEAD = re.compile(r"^### ([a-z0-9_]+) — (.+)$")
_BULLET = re.compile(r"^- ([a-z ]+): (.*)$")


def parse(text: str, doc: str = "") -> list[dict[str, Any]]:
    """Cards in one markdown file. Raises on a malformed card, so a broken doc fails the test, not the seed."""
    cards: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in text.splitlines():
        if m := _HEAD.match(line):
            cur = {"id": m.group(1), "name": m.group(2).strip(), "doc": doc}
            cards.append(cur)
        elif cur is not None and (m := _BULLET.match(line)):
            cur[m.group(1).strip().replace(" ", "_")] = m.group(2).strip()
        elif line.startswith("#"):
            cur = None                                  # a non-card heading ends the card
    for c in cards:
        missing = [k for k in REQUIRED if not c.get(k)]
        if missing:
            raise ValueError(f"{doc}: card {c['id']} missing {missing}")
        if c["direction"].split()[0] not in DIRECTIONS:
            raise ValueError(f"{doc}: card {c['id']} direction must start with one of {DIRECTIONS}")
    return cards


def load(lib_dir: Path = LIB_DIR) -> list[dict[str, Any]]:
    cards = [c for f in sorted(lib_dir.glob("*.md")) for c in parse(f.read_text(), f"docs/strategies/{f.name}")]
    dupes = {c["id"] for c in cards if sum(x["id"] == c["id"] for x in cards) > 1}
    if dupes:
        raise ValueError(f"duplicate card ids: {sorted(dupes)}")
    return cards


def seed_library(db, lib_dir: Path = LIB_DIR) -> dict[str, int]:
    """Idempotent: an unchanged card is a no-op; edited RULES (RULE_KEYS) become a new version, while evidence, grade,
    risks and fit live in the doc and can be updated as studies come in without orphaning episodes or trials."""
    R.ensure(db)
    out = {"cards": 0, "new": 0}
    for c in load(lib_dir):
        body = {k: c[k] for k in RULE_KEYS if c.get(k)}
        r = R.register(db, {"id": f"lib.{c['id']}", "level": "strategy", "name": c["name"],
                            "mechanism": c["mechanism"], "definition": json.dumps(body, sort_keys=True),
                            "detector": "none (library card)", "metric": "trade_r", "status": "draft",
                            "source": f"{c['doc']}#{c['id']}"})
        out["cards"] += 1
        out["new"] += r["new"]
    return out


def table(lib_dir: Path = LIB_DIR) -> str:
    """Markdown index of every card (docs/strategies/README.md): `python -m shortbot.library_seed`."""
    rows = ["| id | direction | family | grade | fit | file |", "|---|---|---|---|---|---|"]
    for c in sorted(load(lib_dir), key=lambda c: (c["direction"], c["family"], c["id"])):
        rows.append(f"| {c['id']} | {c['direction']} | {c['family']} | {c['grade']} | {c['fit'].split(' ')[0]} |"
                    f" {c['doc'].rsplit('/', 1)[-1]} |")
    return "\n".join(rows)


if __name__ == "__main__":
    print(table())

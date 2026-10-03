"""#43: tests never read or write the live desk's data/ (tests/data_guard.py, conftest.isolated_data)."""

import os
import sqlite3
from pathlib import Path

import pytest

from shortbot import long_shadow, provenance, trial_register
from shortbot.market import streams
from tests import data_guard as G


def _outside(p: Path | str) -> bool:
    p = Path(p).resolve()
    return p != G.REPO_DATA and G.REPO_DATA not in p.parents


def test_every_data_path_points_into_the_test_temp_dir(isolated_data):
    assert Path.cwd() == isolated_data != G.REPO_DATA.parent
    assert provenance.data_dir() == isolated_data / "data"
    for p in (provenance.desk_db(), long_shadow.db_path(), trial_register.path(), streams.DB_PATH,
              "data/hist.db"):
        assert _outside(p) and Path(p).resolve().is_relative_to(isolated_data), p


def test_snapshot_reports_what_was_created_modified_and_removed(tmp_path):
    (tmp_path / "keep.db").write_text("a")
    (tmp_path / "gone.db").write_text("b")
    before = G.snapshot(tmp_path)
    assert G.changes(before, G.snapshot(tmp_path)) == [] and G.snapshot(tmp_path / "missing") == {}
    (tmp_path / "gone.db").unlink()
    (tmp_path / "keep.db").write_text("aa")
    (tmp_path / "research").mkdir()
    assert G.changes(before, G.snapshot(tmp_path)) == ["created research", "modified .", "modified keep.db",
                                                       "removed gone.db"]


def test_the_guard_refuses_every_kind_of_write_under_its_root_and_records_it(tmp_path):
    root, other = tmp_path / "data", tmp_path / "elsewhere"
    g = G.Guard(root)
    g.test = "t::x"
    writes = [("open", (str(root / "a.db"), "w", 0)), ("open", (root / "a.log", "a", 0)),
              ("open", (str(root / "a"), None, os.O_WRONLY | os.O_CREAT)), ("sqlite3.connect", (str(root / "j.db"),)),
              ("sqlite3.connect", (f"file:{root}/j.db?mode=ro",)), ("os.mkdir", (str(root / "r"), 0o777, None)),
              ("os.remove", (str(root / "a"), None)), ("os.rename", (str(other / "x"), str(root / "x"), None, None)),
              ("shutil.copyfile", (str(other / "x"), str(root / "x")))]
    for event, args in writes:
        with pytest.raises(PermissionError, match="#43"):
            g(event, args)
    assert len(g.seen) == len(writes) and g.seen[0] == f"t::x: open {root / 'a.db'}"
    for event, args in [("open", (str(root / "a.db"), "r", 0)), ("open", (str(other / "a.db"), "w", 0)),
                        ("open", (3, "w", 0)), ("sqlite3.connect", (":memory:",)), ("import", ("os",)),
                        ("os.rename", (str(other / "x"), str(other / "y"), None, None))]:
        g(event, args)
    assert len(g.seen) == len(writes)


def test_the_session_guard_is_installed():
    """Probed with writes that can't land even unguarded (the parent dir doesn't exist); the record is dropped so this
    test's own probe doesn't fail the run."""
    probe = G.REPO_DATA / "no-such-dir" / "probe.db"
    n = len(G.SESSION.seen)
    with pytest.raises(PermissionError, match="#43"):
        open(probe, "w")
    with pytest.raises(PermissionError, match="#43"):
        sqlite3.connect(f"file:{probe}?mode=ro", uri=True)
    assert len(G.SESSION.seen) == n + 2
    del G.SESSION.seen[n:]

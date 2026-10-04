"""Guardian uptime (#46, uptime.py): each verified heartbeat is compared with the one before it (this process's, or the
previous process's kv value), a gap over GAP_S is a guardian_gaps row tagged `drill` inside a drill window, kv
uptime_since marks the first stamp; the writes are posted, so a held journal lock never delays the pass."""

import time
from types import SimpleNamespace

from shortbot import killswitch as ks
from shortbot import uptime
from shortbot.guardian import Guardian
from shortbot.journal import Journal
from tests.fakes import FakeCcxt
from tests.held_lock import STALL_REPORT, HeldLock
from tests.test_guardian import guardian_with, open_trade
from tests.test_guardian_offloop import guardian_on

T0 = 1_790_000_000.0


def beat(g, at):
    Guardian._heartbeat(g, at)


def stub(j):
    """The guardian as _heartbeat sees it: its lanes (none failing), its journal, its last stamp."""
    return SimpleNamespace(lanes={}, j=j, _last_beat=None)


def test_the_first_stamp_sets_uptime_since_and_a_short_restart_is_no_gap():
    j = Journal(":memory:")
    j.set(ks.HEARTBEAT_KEY, str(T0 - 40))     # the previous process stamped 40 s before
    g = stub(j)
    beat(g, T0)
    beat(g, T0 + 60)
    beat(g, T0 + 120)
    assert uptime.uptime_since(j) == uptime.iso(T0) and uptime.gaps(j) == []
    assert float(j.get(ks.HEARTBEAT_KEY)) == T0 + 120
    beat(stub(j), T0 + 200)                   # a restart: the next process's first stamp sets nothing new
    assert uptime.uptime_since(j) == uptime.iso(T0)


def test_a_gap_since_the_previous_process_is_a_row_from_its_last_stamp():
    j = Journal(":memory:")
    j.set(ks.HEARTBEAT_KEY, str(T0 - 900))    # down 15 min
    beat(stub(j), T0)
    (r,) = uptime.gaps(j)
    assert (r["start_at"], r["end_at"], r["secs"], r["mode"], r["tag"]) == (
        uptime.iso(T0 - 900), uptime.iso(T0), 900.0, "demo", None)
    assert r["pid"] > 0


def test_gaps_within_a_process_count_from_its_last_stamp_and_only_past_gap_s():
    j = Journal(":memory:")
    g = stub(j)
    beat(g, T0)                                # never stamped before: no gap
    beat(g, T0 + uptime.GAP_S)                 # exactly GAP_S: not a gap
    beat(g, T0 + uptime.GAP_S + 181)
    (r,) = uptime.gaps(j)
    assert r["secs"] == 181.0 and r["start_at"] == uptime.iso(T0 + uptime.GAP_S)


def test_a_gap_inside_a_drill_window_is_tagged_and_one_outside_is_not():
    j = Journal(":memory:")
    g = stub(j)
    beat(g, T0)
    j.run(uptime.open_window, "restart", 3600, T0 + 100)
    beat(g, T0 + 900)                          # overlaps the window: the planned outage
    j.run(uptime.close_window, "restart", T0 + 1000)
    beat(g, T0 + 1000)
    beat(g, T0 + 2000)                         # after the window closed: unexplained
    assert [(r["secs"], r["tag"]) for r in uptime.gaps(j)] == [(900.0, "drill"), (1000.0, None)]
    assert uptime.windows(j) == {"restart": {"from": T0 + 100, "until": T0 + 1000}}
    s = uptime.summary(j, now=T0 + 2010)
    assert (s["downtime_s"], s["untagged_max_s"], s["open_gap_s"], s["covered"]) == (1900.0, 1000.0, None, True)
    assert uptime.summary(j, since=uptime.iso(T0 + 1500))["gaps"][0]["secs"] == 1000.0   # only gaps ending after it


def test_a_live_journal_ignores_demo_drill_windows():
    live = Journal(":memory:", "live")
    live.run(uptime.open_window, "restart", 3600, T0)      # live's own key: tagged
    live.set(uptime.WINDOW_KEY + "kill-switch", '{"from": 0, "until": 9e12}')   # a demo window in the shared file
    assert set(uptime.windows(live)) == {"restart"}
    live.run(uptime.close_window, "restart", T0)
    g = stub(live)
    beat(g, T0 + 10)
    beat(g, T0 + 900)
    assert [r["tag"] for r in uptime.gaps(live)] == [None]
    assert uptime.uptime_since(live) == uptime.iso(T0 + 10) and live.get("uptime_since") is None


def test_a_down_guardian_is_an_open_gap_in_the_summary():
    j = Journal(":memory:")
    assert uptime.summary(j)["covered"] is False
    beat(stub(j), T0)
    s = uptime.summary(j, now=T0 + 700)
    assert s["open_gap_s"] == 700.0 and s["gaps"] == [] and s["uptime_since"] == uptime.iso(T0)


async def test_a_withheld_heartbeat_counts_as_a_gap_once_it_resumes():
    g = guardian_with(FakeCcxt())
    await open_trade(g)
    await g.check_positions()
    first = float(g.j.get(ks.HEARTBEAT_KEY))
    g.lanes = {"protective": SimpleNamespace(failing=lambda job: True)}   # the fill poll keeps failing
    beat(g, first + 400)
    assert float(g.j.get(ks.HEARTBEAT_KEY)) == first and uptime.gaps(g.j) == []
    g.lanes = {}
    beat(g, first + 500)
    (r,) = uptime.gaps(g.j)
    assert r["secs"] == 500.0 and r["start_at"] == uptime.iso(first)


async def test_the_positions_pass_logs_the_outage_of_the_process_before_it():
    g = guardian_with(FakeCcxt())
    g.j.set(ks.HEARTBEAT_KEY, str(time.time() - 1200))   # the last guardian died 20 min ago
    await g.check_positions()
    (r,) = uptime.gaps(g.j)
    assert 1199 <= r["secs"] <= 1260 and r["tag"] is None and uptime.uptime_since(g.j)


@STALL_REPORT
async def test_a_gap_row_never_waits_for_another_process_s_write_lock(tmp_path):
    g = guardian_on(tmp_path / "j.db")
    g.j.set(ks.HEARTBEAT_KEY, str(T0))
    with HeldLock(tmp_path / "j.db", 2.0) as held:
        t0 = time.monotonic()
        beat(g, T0 + 1000)
        assert time.monotonic() - t0 < 0.5      # posted: the pass goes on
        assert Journal(tmp_path / "j.db")._rows("SELECT * FROM guardian_gaps") == []
        held.wait()
    await g.j.flush()
    assert [r["secs"] for r in uptime.gaps(g.j)] == [1000.0] and float(g.j.get(ks.HEARTBEAT_KEY)) == T0 + 1000

"""Market clock (shortbot.clock): live = the wall clock; a replay pins it so signals, stores and the scanner see
history as of that moment, never after it (task #29)."""

import ast
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

from shortbot import clock
from shortbot.market.events import EventStore
from shortbot.market.liquidations import LiquidationStore
from shortbot.market.social import SocialStore

T = 1_790_000_000_000          # 2026-09-21T14:13:20Z
SRC = Path(__file__).resolve().parents[1] / "src" / "shortbot"


def test_default_is_the_wall_clock():
    assert abs(clock.now_ms() - time.time() * 1000) < 1000
    assert abs(clock.now_dt().timestamp() - time.time()) < 1
    assert clock.now_dt().tzinfo is not None
    assert not clock.replaying()


def test_at_pins_and_restores():
    with clock.at(T):
        assert clock.now_ms() == T and clock.replaying()
        assert clock.now_dt() == datetime.fromtimestamp(T / 1000, timezone.utc)
        with clock.at(T + 5):
            assert clock.now_ms() == T + 5
        assert clock.now_ms() == T
    assert not clock.replaying()


def test_tasks_inherit_the_pinned_clock():
    async def seen():
        await asyncio.sleep(0)
        return clock.now_ms()

    async def main():
        with clock.at(T):
            return await asyncio.gather(seen(), seen())
    assert asyncio.run(main()) == [T, T]


def test_liquidations_never_return_rows_after_now(tmp_path):
    s = LiquidationStore(tmp_path / "j.db")
    s.record([(T - 1000, "X/USDT:USDT", "long_liquidated", 1, 1, 10), (T + 1000, "X/USDT:USDT", "long_liquidated", 1, 1, 99)])
    with clock.at(T):
        rows = asyncio.run(s.recent("X/USDT:USDT", T - 3_600_000))
    assert [r["notional"] for r in rows] == [10]
    assert len(asyncio.run(s.recent("X/USDT:USDT", T - 3_600_000))) == 2   # live: everything up to the wall clock


def test_social_series_never_returns_rows_after_now(tmp_path):
    s = SocialStore(tmp_path / "j.db")
    s.record([(T - 1000, "X", "square", "posts_7d", 1.0), (T + 1000, "X", "square", "posts_7d", 2.0)])
    with clock.at(T):
        assert s.series("X", "posts_7d", 0) == [(T - 1000, 1.0)]


def test_unlocks_are_point_in_time(tmp_path):
    s = EventStore(tmp_path / "j.db")
    s.add_unlock("ABC", "2026-10-01T00:00:00+00:00", 5.0, "team", "https://example.org")   # added now (2026-10+)
    with clock.at(T):
        assert s.upcoming("ABC") == []          # recorded after T: unknown at T
    s.db.execute("UPDATE unlocks SET added_at='2026-09-01T00:00:00+00:00'")
    with clock.at(T):
        ev = s.upcoming("ABC")
    assert len(ev) == 1 and abs(ev[0]["days_to_unlock"] - (1_790_812_800_000 - T) / 86_400_000) < 0.01


def test_market_map_cache_is_as_of_now(tmp_path):
    import json

    from shortbot.market.market_map import KV_LAST, MapCache
    p = tmp_path / "j.db"
    m = MapCache(p)
    m.db.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")
    m.db.execute("INSERT INTO kv VALUES (?,?)", (KV_LAST, json.dumps({"ts": T // 1000, "coins": {}})))
    with clock.at(T + 600_000):
        assert m.get() is not None
    with clock.at(T + 7_200_000):
        assert m.get() is None                  # stale
    with clock.at(T - 600_000):
        assert m.get() is None                  # built after now: look-ahead


# ------------------------------------------------------------------ guard: no wall-clock reads in market time
WALL = {("time", "time"), ("time", "time_ns"), ("datetime", "now"), ("datetime", "utcnow"), ("date", "today")}


def _wall_calls(path: Path, root: Path = SRC) -> list[str]:
    src = path.read_text()
    lines = src.splitlines()
    out = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        f = node.func
        owner = f.value.id if isinstance(f.value, ast.Name) else f.value.attr if isinstance(f.value, ast.Attribute) else None
        bare_gm = owner == "time" and f.attr in ("gmtime", "localtime") and not node.args
        if ((owner, f.attr) in WALL or bare_gm) and "# wall-clock" not in lines[node.lineno - 1]:
            out.append(f"{path.relative_to(root)}:{node.lineno}")
    return out


def test_pipeline_reads_market_time_only_through_the_clock():
    """signals/, market/ and the scanner must ask shortbot.clock for 'now' (a replay pins it). A genuine wall-clock
    use (cache TTL, rate limiter, recorder timestamp, build timing) says so with a `# wall-clock` comment."""
    files = [*sorted((SRC / "signals").glob("*.py")), *sorted((SRC / "market").glob("*.py")), SRC / "scanner.py"]
    bad = [hit for f in files for hit in _wall_calls(f)]
    assert not bad, f"direct wall-clock reads (use shortbot.clock, or mark '# wall-clock'): {bad}"


def test_guard_catches_a_direct_call(tmp_path):
    p = tmp_path / "probe.py"
    p.write_text("import time\nfrom datetime import datetime\nx = time.time()\ny = datetime.now()\n"
                 "z = time.time()  # wall-clock: cache TTL\n")
    assert _wall_calls(p, tmp_path) == ["probe.py:3", "probe.py:4"]

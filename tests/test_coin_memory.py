"""Coin memory's store (coin_memory.py): the focus set, append-only reads that expire, the read score, the watchlist,
the brief's focus-coin lines (no third-party text) and the three tools with CLI parity."""

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from shortbot import clock, cmd_desk, cmd_research
from shortbot import coin_memory as cm
from shortbot.cli_brief import brief
from shortbot.commands import CommandError
from shortbot.journal import Journal
from shortbot.mcp_server import TOOLS
from tests.test_brief import app
from tests.test_cli_fallback import _both
from tests.test_untrusted import EVIL, PAYLOAD

NOW = 1_790_000_000_000
H = 3_600_000
QNT, SUI, ZRO = "QNT/USDT:USDT", "SUI/USDT:USDT", "ZRO/USDT:USDT"


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def trade(j, sym=QNT):
    tid = j.create_trade(sym, "pump_fade", "t", 1.1, 2, plan={})
    j.activate_trade(tid, 1.0, 100.0, 100.0)
    return tid


def pending(*rows):
    return SimpleNamespace(pending=lambda: [{"id": i, "symbol": s, "limit_price": p} for i, s, p in rows])


def read(j, sym=QNT, text="post-pump chop, retail long", bias="down", at=NOW, hours=24.0, price=250.0, **kw):
    return j.run(cm.add_read, sym, text, bias, kw.get("levels"), kw.get("invalidation"), hours, price, at,
                 {"actor": "test"})


def state(regime="active", rel_vol=1.9, events=(), brk=None):
    st = {"price": 1.0, "regime": {"state": regime, "rel_vol": rel_vol}, "fakeout_p90": 0.9,
          "swing_5m": {"median_pct": 2.1, "median_min": 40}, "events": [{"name": e, "detail": "x"} for e in events]}
    return {**st, "break": brk} if brk else st


# ------------------------------------------------------------ the focus set
def test_the_focus_set_is_trades_entries_watchlist_then_the_last_hours_top_candidates():
    j = Journal(":memory:")
    tid = trade(j)
    j.log_candidates(iso(NOW), NOW - 30 * 60_000, [{"symbol": f"C{i}/USDT:USDT", "score": 10 + i} for i in range(7)]
                     + [{"symbol": "VETO/USDT:USDT", "score": 99, "vetoes": ["squeeze"]}], "ok")
    j.log_candidates(iso(NOW), NOW - 2 * H, [{"symbol": "OLD/USDT:USDT", "score": 99}], "ok")
    j.run(cm.watch, "ZRO", False, "range forming", 7, NOW)
    f = cm.focus_symbols(j, pending((3, SUI, 2.0), (4, QNT, 260.0)), NOW)
    assert list(f) == [QNT, SUI, ZRO] + [f"C{i}/USDT:USDT" for i in (6, 5, 4, 3, 2)]
    assert f[QNT] == [f"trade #{tid}", "entry #4"] and f[SUI] == ["entry #3"] and f[ZRO] == ["watch"]
    assert f["C6/USDT:USDT"] == ["scan 16"]
    assert list(cm.focus_symbols(j, None, NOW, cap=2)) == [QNT, ZRO]
    assert ZRO not in cm.focus_symbols(j, None, NOW + 8 * 24 * H)               # the watch ran out
    j.run(cm.watch, "ZRO", True, None, 7, NOW)
    assert ZRO not in cm.focus_symbols(j, None, NOW)


def test_the_watchlist_validates_and_reports():
    j = Journal(":memory:")
    assert cm.focus_symbols(j, None, NOW) == {}                                 # no tables yet: nothing
    r = j.run(cm.watch, "zro", False, "range\nforming", 3, NOW)
    assert r == {"ok": True, "symbol": ZRO, "until": iso(NOW + 3 * 24 * H)}
    assert cm.watching(j.db, NOW)[0]["reason"] == "range forming"
    with pytest.raises(ValueError, match="days"):
        j.run(cm.watch, "ZRO", False, None, 60, NOW)
    assert j.run(cm.watch, "SUI", True, None, 7, NOW) == {"ok": True, "symbol": SUI, "removed": False}


# ------------------------------------------------------------ reads
def test_reads_are_append_only_and_the_newest_unexpired_one_is_current():
    j = Journal(":memory:")
    assert cm.current_read(j.db, QNT, NOW) is None and cm.reads(j.db, QNT) == []
    r1 = read(j, levels={"resistance": 251.2, "support": [242, 236]}, invalidation=251.5)
    r2 = read(j, text="flush done", bias="up", at=NOW + H, hours=6, price=245.0)
    cur = cm.current_read(j.db, QNT, NOW + 2 * H)
    assert cur["id"] == r2["read_id"] and cur["bias"] == "up" and cur["price_at"] == 245.0
    assert cm.current_read(j.db, QNT, NOW + 8 * H) is None       # the newest expired: the older one stays archived
    old = cm.reads(j.db, QNT)[1]
    assert old["id"] == r1["read_id"] and old["levels"] == {"resistance": 251.2, "support": [242, 236]}
    assert old["invalidation"] == 251.5 and old["actor"] == "test" and r1["expires"] == iso(NOW + 24 * H)
    for sql in ("UPDATE coin_reads SET bias='up'", "DELETE FROM coin_reads"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            j.db.execute(sql)


@pytest.mark.parametrize("kw, why", [
    ({"text": "x" * 1201}, "1-1200"), ({"text": "  "}, "1-1200"), ({"bias": "short"}, "bias"),
    ({"hours": 100}, "expires_h"), ({"hours": 0}, "expires_h"), ({"invalidation": -1}, "invalidation"),
    ({"levels": {"r": "high"}}, "levels"), ({"levels": [251]}, "levels"), ({"levels": {"r": True}}, "levels")])
def test_a_bad_read_is_refused(kw, why):
    j = Journal(":memory:")
    with pytest.raises(ValueError, match=why):
        read(j, **kw)
    assert cm.reads(j.db, QNT) == []


def test_read_score_scores_expired_reads_by_the_move_from_price_at():
    j = Journal(":memory:")
    hit = read(j, bias="down", price=250.0)["read_id"]
    miss = read(j, sym=SUI, bias="up", price=2.0)["read_id"]
    flat = read(j, sym=ZRO, bias="neutral", price=4.0)["read_id"]
    read(j, sym=ZRO, bias="up", price=4.0)                                       # not scored yet
    assert [r["id"] for r in cm.due_outcomes(j.db, NOW + 25 * H)] == [hit, miss, flat, flat + 1]
    j.run(cm.save_outcomes, [(hit, NOW + 24 * H, 240.0), (miss, NOW + 24 * H, 1.9), (flat, NOW + 24 * H, 4.1)])
    s = cm.read_score(j.db)
    assert s["directional"] == {"n": 2, "hits": 1, "hit_rate": 0.5} and s["neutral"]["median_abs_move_pct"] == 2.5
    assert cm.read_score(j.db, QNT)["directional"] == {"n": 1, "hits": 1, "hit_rate": 1.0}
    assert [r["id"] for r in cm.due_outcomes(j.db, NOW + 25 * H)] == [flat + 1]


# ------------------------------------------------------------ the brief
def test_brief_lines_show_fresh_focus_coins_trades_first_and_never_the_read_text():
    j = Journal(":memory:")
    coins = {f"C{i}/USDT:USDT": state() for i in range(9)}
    coins[QNT] = state("trend_down", 2.4, ("retail_flip",), {"score": 2})
    focus = {s: ["scan 20"] for s in coins} | {QNT: ["trade #1"]}
    j.run(cm.save, coins, focus, NOW)
    j.run(cm.save, {"OLD/USDT:USDT": state()}, {"OLD/USDT:USDT": ["watch"]}, NOW - 3 * H)
    read(j, text=f"evil {EVIL}", at=NOW - 3 * H)
    lines = cm.brief_lines(j, NOW + 5 * 60_000)
    assert lines[0] == "### focus coins (10, coin memory pass 5m ago; `coin_memory` for more)"
    assert len(lines) == 1 + cm.BRIEF_LINES and "OLD" not in "\n".join(lines)
    assert lines[1] == ("- QNT trend_down vol 2.4x | swing 2.1%/40m | fakeout p90 0.9% | read 3h (down) | break 2/4 "
                        "| events: retail_flip")
    assert lines[2] == "- C0 active vol 1.9x | swing 2.1%/40m | fakeout p90 0.9% | no read"
    assert PAYLOAD not in "\n".join(lines) and cm.brief_lines(Journal(":memory:")) == []


async def test_the_brief_carries_the_focus_coins(monkeypatch):
    a = app()
    a.journal.run(cm.save, {QNT: state()}, {QNT: ["watch"]}, int(clock.now_ms()))
    read(a.journal, text=f"x {EVIL}", at=clock.now_ms())
    text = await brief(a)
    assert "### focus coins (1," in text and "- QNT active vol 1.9x" in text and PAYLOAD not in text

    def broken(j):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(cm, "brief_lines", broken)
    assert "⚠️ could not load coin memory: OperationalError: disk I/O error" in await brief(a)


# ------------------------------------------------------------ tools
async def test_coin_memory_lists_the_focus_set_and_shows_one_coin_with_untrusted_headlines(monkeypatch):
    a = app()
    monkeypatch.setattr(cmd_desk, "get_app", lambda: a)
    j, now = a.journal, clock.now_ms()
    trade(j)
    j.run(cm.save, {QNT: {**state(), "news": 2, "series": [[now, 1.0, 1, 1, 1]] * 3}}, {QNT: ["trade #1"]}, now)
    read(j, at=now)
    out = await TOOLS["coin_memory"]()
    assert [f["symbol"] for f in out["focus"]] == [QNT] and out["focus"][0]["line"].startswith("QNT active")
    assert out["watchlist"] == [] and out["read_score"]["directional"]["n"] == 0

    async def news_for(base, max_items=15):
        return {"coin": base, "binance_announcements": [], "headlines": [{"title": EVIL, "url": "", "summary": "",
                                                                          "source": "x"}], "errors": []}
    monkeypatch.setattr("shortbot.news.news_for", news_for)
    one = await TOOLS["coin_memory"]("QNT")
    assert one["symbol"] == QNT and one["read"]["bias"] == "down" and one["series"]["points"] == 3
    assert "series" not in one["state"] and len(one["reads"]) == 1 and one["focus"] == ["trade #1"]
    assert set(one["news"]["headlines"][0]["title"]) == {"untrusted_text", "source"}


async def test_update_coin_read_takes_the_ticker_price_and_refuses_a_bad_read(monkeypatch):
    a = app()
    monkeypatch.setattr(cmd_desk, "get_app", lambda: a)
    a.ex.fake.add_symbol(QNT, 250.0)
    r = await cmd_desk.update_coin_read("QNT", "chop", "down", {"resistance": 251.2}, 251.5)
    assert r["ok"] and r["price_at"] == 250.0 and cm.current_read(a.journal.db, QNT, clock.now_ms())["bias"] == "down"
    with pytest.raises(CommandError, match="bias"):
        await cmd_desk.update_coin_read("QNT", "chop", "sideways")
    with pytest.raises(CommandError, match="days"):
        await cmd_desk.watch("QNT", days=90)


def test_the_tools_declare_the_stores_choices_and_defaults():
    import inspect
    from shortbot import commands
    commands.load()
    bias = next(a for a in commands.by_cli("coin-read").args if a.name == "bias")
    assert bias.choices == cm.BIASES
    sig = inspect.signature(cmd_desk.update_coin_read)
    assert sig.parameters["expires_h"].default == cm.READ_TTL_H


async def test_update_coin_read_and_watch_parity(tmp_path, monkeypatch):
    monkeypatch.setattr(cmd_desk, "get_app", lambda *a, **k: cmd_research.get_app())   # follows _both's patching
    with clock.at(NOW):
        res_mcp, res_cli, calls = await _both(
            tmp_path, monkeypatch, lambda a: None,
            lambda: TOOLS["update_coin_read"]("QNT", "post-pump chop", "down", levels={"resistance": 251.2},
                                              invalidation=251.5),
            ["coin-read", "QNT", "post-pump chop", "--bias", "down", "--levels", '{"resistance": 251.2}',
             "--invalidation", "251.5"])
        assert res_mcp == res_cli and res_mcp["ok"] and res_mcp["price_at"] is None   # no ticker, no state
        assert calls == {"mcp": [("mcp:update_coin_read", "done")], "cli": [("cli:coin-read", "done")]}
        res_mcp, res_cli, calls = await _both(
            tmp_path / "watch", monkeypatch, lambda a: None, lambda: TOOLS["watch"]("ZRO", reason="range", days=3),
            ["watch", "ZRO", "--reason", "range", "--days", "3"])
        assert res_mcp == res_cli == {"ok": True, "symbol": ZRO, "until": iso(NOW + 3 * 24 * H)}
        assert calls["cli"] == [("cli:watch", "done")]

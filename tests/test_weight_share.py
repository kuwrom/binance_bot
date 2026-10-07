"""The host-wide ledger never holds anyone up (weight_share.py): protective calls never touch it, a contended ledger
costs a desk/research call at most weight_ledger.BUSY_MS per operation and falls back to local readings, pacing holds
in-process when the ledger is off or broken, and a script in a git worktree finds the main checkout's ledger."""

import sqlite3
import subprocess
import threading
import time

import pytest

from shortbot import http, weight, weight_ledger, weight_share
from shortbot.weight import DESK, PROTECTIVE, RESEARCH
from tests.fake_http import FakeHost, fake_clock

FAPI = "https://fapi.binance.com/fapi/v1/klines"
BAN = {"x-mbx-used-weight-1m": "2400", "retry-after": "600"}


@pytest.fixture(autouse=True)
def fresh():
    weight.reset()
    yield
    weight.reset()


def test_a_protective_call_never_waits_on_the_ledger_even_when_it_hangs(monkeypatch):
    touched = []

    def hang(*a, **k):
        touched.append(threading.current_thread().name)
        time.sleep(0.3)
    for name in ("state", "note", "reserve", "done"):
        monkeypatch.setattr(weight_ledger.Ledger, name, hang)
    t0 = time.monotonic()
    with weight.tagged(PROTECTIVE):
        weight.release(weight.admit_blocking(FAPI))
        weight.record(FAPI, 200, {"x-mbx-used-weight-1m": "7"}, "")      # an ordinary reading: stays local
        weight.record(FAPI, 418, BAN, '{"code":-1003}')                   # a ban: shared, in the background
    with weight.tagged(DESK):
        weight.admit_blocking("https://fapi.binance.com/fapi/v1/order", "POST")   # a write: the scope turns protective
        weight.record("https://fapi.binance.com/fapi/v1/order", 200, {"x-mbx-used-weight-1m": "5"}, "{}")
    assert time.monotonic() - t0 < 0.05 and weight.budget("fapi.binance.com").last_limit == "HTTP 418"
    weight_share.flush()
    assert touched == ["weight-ledger"]                                  # only the background writer, once


def test_a_protective_ban_reaches_the_other_processes(monkeypatch):
    with weight.tagged(PROTECTIVE):
        weight.record(FAPI, 418, BAN, '{"code":-1003}')
    weight.reset()                                                       # flushes, then forgets: another process
    with pytest.raises(weight.BudgetWait, match="418"):
        with weight.tagged(RESEARCH, wait_s=0):
            weight.admit_blocking(FAPI)
    with weight.tagged(PROTECTIVE):
        assert weight.admit_blocking(FAPI) is None


def test_a_contended_ledger_costs_a_call_milliseconds_and_falls_back(tmp_path):
    weight.record(FAPI, 200, {"x-mbx-used-weight-1m": "10"}, "")       # creates the ledger
    led = weight_ledger.get()
    other = sqlite3.connect(led.path, isolation_level=None)
    other.execute("BEGIN EXCLUSIVE")                                     # another process mid-write
    try:
        t0 = time.monotonic()
        with weight.tagged(RESEARCH, wait_s=0, paced=True):
            weight.release(weight.admit_blocking(FAPI))                  # reserve: busy -> local pacing
        with weight.tagged(DESK):
            weight.record(FAPI, 200, {"x-mbx-used-weight-1m": "20"}, "")   # dropped
        assert time.monotonic() - t0 < 0.2
        assert weight.budget("fapi.binance.com").used == 20              # the local reading still counts
    finally:
        other.execute("ROLLBACK")
        other.close()
    held = threading.Event()
    release = threading.Event()

    def hold():
        with led._session():
            held.set()
            release.wait(5)
    t = threading.Thread(target=hold)
    t.start()
    held.wait(5)
    try:
        t0 = time.monotonic()
        with weight.tagged(RESEARCH, wait_s=0):
            weight.release(weight.admit_blocking(FAPI))                  # another thread holds it: no wait
        assert time.monotonic() - t0 < 0.2
    finally:
        release.set()
        t.join(5)


@pytest.mark.parametrize("ledger", ["off", "broken"])
def test_pacing_holds_in_process_when_the_ledger_is_off_or_broken(ledger, monkeypatch, tmp_path):
    if ledger == "broken":
        (tmp_path / "dir.db").mkdir()
        monkeypatch.setenv(weight_ledger.ENV, str(tmp_path / "dir.db"))
    else:
        monkeypatch.setenv(weight_ledger.ENV, "off")
    clock = fake_clock(monkeypatch, weight)
    host, sent = FakeHost("fapi.binance.com", 2400, step=0), []

    def send(url, timeout):
        sent.append(clock.t)
        r = host.respond("GET", "/fapi/v1/klines")
        return r.status, r.headers, r._text
    monkeypatch.setattr(http, "_send", send)
    for _ in range(60):                                                   # 300 weight at 5 each
        http.get("/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 1000}, wait_s=1e9)
    win, cap = weight_share.PACE_WINDOW_S, weight_share.PACE_WEIGHT_CAP
    assert max(sum(5 for t in sent if t0 <= t < t0 + win) for t0 in sent) <= cap
    assert sent[-1] - sent[0] >= win


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


def test_the_ledger_follows_the_codes_checkout_never_the_cwd(tmp_path, monkeypatch):
    main, wt, elsewhere = tmp_path / "binance_bot", tmp_path / "wt-x", tmp_path / "scratch"
    for d in (main / "src" / "shortbot", main / "data", elsewhere):
        d.mkdir(parents=True)
    (main / "pyproject.toml").write_text("[project]\nname = 'shortbot'\n")
    _git("init", "-q", cwd=main)
    _git("add", "-A", cwd=main)
    _git("commit", "-q", "-m", "init", cwd=main)
    _git("worktree", "add", "-q", str(wt), cwd=main)
    assert weight_ledger.main_checkout(wt) == main.resolve()
    assert weight_ledger.main_checkout(main) is None and weight_ledger.main_checkout(tmp_path) is None
    monkeypatch.chdir(elsewhere)                                          # a script run from the scratchpad
    monkeypatch.delenv("SHORTBOT_DB")
    want = main.resolve() / "data" / weight_ledger.FILENAME
    for root in (wt, main):                                               # the worktree's code, the main venv's
        monkeypatch.setattr(weight_ledger, "_paths", {})
        monkeypatch.setattr(weight_ledger, "CODE_ROOT", root)
        assert weight_ledger.path() == want
    monkeypatch.setenv("SHORTBOT_DB", "data/shortbot.db")                 # relative: the checkout's, not the cwd's
    assert weight_ledger.path() == want
    monkeypatch.setenv("SHORTBOT_DB", str(tmp_path / "app" / "data" / "shortbot.db"))   # Docker: /app/data
    assert weight_ledger.path() == (tmp_path / "app" / "data").resolve() / weight_ledger.FILENAME
    monkeypatch.setenv(weight_ledger.ENV, str(tmp_path / "x.db"))
    assert weight_ledger.path() == (tmp_path / "x.db").resolve()
    monkeypatch.delenv(weight_ledger.ENV)
    monkeypatch.delenv("SHORTBOT_DB")
    monkeypatch.setattr(weight_ledger, "_paths", {})
    monkeypatch.setattr(weight_ledger, "CODE_ROOT", tmp_path / "site-packages")   # an installed copy
    assert weight_ledger.path() is None and not (elsewhere / "data").exists()

"""The proxy backtest's liquidity floor (#73): the live floor by default, --min-vol24 1e6 reproduces rounds 0-4."""

import pytest

from shortbot.backtest import run
from shortbot.backtest import strategies as S
from shortbot.config import RiskLimits
from tests.test_backtest import T0, _synthetic
from tests.test_provenance import _write_hist

H = 3_600_000


@pytest.fixture
def floor(monkeypatch):
    monkeypatch.setattr(S, "MIN_VOL24", S.MIN_VOL24)      # restored after the test
    seen = []
    real = run.run_all

    def spy(m, *a, **k):
        seen.append(S.MIN_VOL24)
        return real(m, *a, **k)
    monkeypatch.setattr(run, "run_all", spy)
    return seen


def test_default_is_the_live_floor_and_1e6_reproduces_the_old_rounds(tmp_path, monkeypatch, floor):
    monkeypatch.setenv("SHORTBOT_DB", str(tmp_path / "data" / "shortbot.db"))
    hist = tmp_path / "data" / "hist.db"
    _write_hist(hist, _synthetic())
    run.main(["--days60", "--db", str(hist), "--no-save"])
    assert floor[0] == RiskLimits().min_quote_volume_24h == 20e6
    run.main(["--days60", "--db", str(hist), "--no-save", "--min-vol24", "1e6"])
    assert floor[-1] == 1e6


def test_the_floor_is_read_when_a_signal_is_checked(monkeypatch):
    monkeypatch.setattr(S, "MIN_VOL24", S.MIN_VOL24)
    rows = [{"ts": T0 + k * H, "o": 1, "h": 1, "l": 1, "c": 1, "qv": 0.5e6} for k in range(S.WARMUP + 30)]
    coin = S.Coin("X/USDT:USDT", rows, 0, [], "other", [], {})
    i = S.WARMUP + 25                                   # 24h volume 12M
    S.set_min_vol24(20e6)
    assert not S.tradable(coin, i)
    S.set_min_vol24(1e6)
    assert S.tradable(coin, i)
    from shortbot.backtest import strategies_more as SM
    assert SM.floor() == 1e6


def test_label_names_a_non_live_floor():
    a = run.argparse.Namespace(days60=True, round1=False, round2=False, round3=False, round4=False, long=False,
                               long2=False, placebo="matched", dsr="clustered", min_vol24=1e6)
    assert run.label(a) == "bt60_floor1m"
    assert run.label(run.argparse.Namespace(**{**vars(a), "min_vol24": 20e6})) == "bt60"


def test_rerun_of_a_run_recorded_before_the_flag_keeps_its_1m_floor():
    from shortbot import provenance as P
    old = {"config": {"command": "shortbot.backtest.run", "argv": ["--days60"], "args": {"days60": True}}}
    assert P.rerun_cmd(old)[-2:] == ["--min-vol24", "1000000"]
    new = {"config": {"command": "shortbot.backtest.run", "argv": ["--days60"],
                      "args": {"days60": True, "min_vol24": 20e6}}}
    assert "--min-vol24" not in P.rerun_cmd(new)
    other = {"config": {"command": "shortbot.backtest.trail5m", "argv": [], "args": {}}}
    assert "--min-vol24" not in P.rerun_cmd(other)

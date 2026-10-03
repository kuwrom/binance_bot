"""#31: every study's entry point writes its trials to the one register, in the shapes the old ledger held."""

import ast
from pathlib import Path

import pytest

from shortbot import trial_register as TR
from shortbot.backtest import register_rows as RR
from shortbot.backtest.report import build, verdict
from shortbot.backtest.run import run_all
from shortbot.backtest.strategies import trials
from tests.test_backtest_fixes import _market

SRC = Path(__file__).resolve().parents[1] / "src" / "shortbot"
RECORDERS = {"record", "record_backtest", "record_study"}
# every study entry point: module -> the function that must record its trials
ENTRY = {"backtest/run.py": "_run", "backtest/carry_regime.py": "main", "backtest/funding_events.py": "main",
         "backtest/hedge_report.py": "main", "backtest/rungs.py": "main", "backtest/trail5m.py": "main",
         "backtest/xs_factors.py": "main", "backtest/card_tests.py": "main", "backtest/long_studies.py": "main", "lifecycle_report.py": "main",
         "registry.py": "add_trial", "cli.py": "_study", "backtest/replay_study.py": "main"}
EXEMPT = {"backtest/drawdown.py": "drawdown quantiles of books already tested: selects nothing",
          "sweeps.py": "fake-out size quantiles for stop placement: descriptive, no hypothesis",
          "backtest/trigger_rates.py": "scanner trigger base rates per 15m window: descriptive, selects nothing",
          "backtest/cadence_study.py": "tier-1 evaluation cadence (lead, load, transients): an engineering choice, no edge claim",
          "refresh.py": "rebuilds hist.db and reruns the backtest, whose own main records",
          "backtest/replay_verify.py": "checks the live-pipeline replay against logged scans: tests no strategy (#29)"}


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setenv(TR.ENV, str(tmp_path / "trials.db"))
    monkeypatch.delenv(TR.ASOF_ENV, raising=False)


def _calls(fn: ast.AST) -> set[str]:
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


def test_every_study_entry_point_records_its_trials():
    # a study = a backtest module with a main(), or any module that stamps research outputs (provenance.stamp)
    found = {str(p.relative_to(SRC)) for p in (SRC / "backtest").glob("*.py")
             if any(isinstance(n, ast.FunctionDef) and n.name == "main" for n in ast.parse(p.read_text()).body)}
    found |= {str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if "P.stamp(" in p.read_text()}
    unlisted = found - set(ENTRY) - set(EXEMPT)
    assert not unlisted, f"new study entry point(s) {unlisted}: record their trials (trial_register) or exempt them"
    for mod, fn in ENTRY.items():
        tree = ast.parse((SRC / mod).read_text())
        f = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn)
        assert _calls(f) & RECORDERS, f"{mod}:{fn} does not record its trials"
        src = (SRC / mod).read_text()
        assert "trial_register" in src or "register_rows" in src, mod


def test_backtest_records_every_trial_and_split_cell_under_the_legacy_spec(reg):
    m = _market()
    led = [t for t in trials() if t.strategy in ("pump_fade", "bounce_fade")]
    res = run_all(m, ledger=led, placebo="matched")
    rep = build(res, m, dsr_mode="clustered", register=True)
    rows = RR.backtest_rows(rep, res)
    names = {r["name"] for r in rows if r.get("study") != "backtest.splits"}
    assert names == {t.name for t in led}
    t0 = led[0]
    row = next(r for r in rows if r["name"] == t0.name)
    assert TR.spec_of("backtest", row["family"], row["params"]) == TR.spec_of(
        "backtest", f"strategy.{t0.strategy}", {"trial": t0.name, **t0.params})     # registry_seed's bt60 shape
    traded = [r for r in rows if r["n"]]
    assert traded and all(r["daily"] and r["t_kind"] == "2way" for r in traded)
    assert any(r.get("study") == "backtest.splits" for r in rows)
    n = RR.record_backtest(rep, res)
    s = TR.summary()
    assert n == len(rows) and s["by_study"]["backtest"] == len(led) and s["by_study"]["backtest.splits"] > 0
    assert rep["register"]["specs"] == 0 and "n_eff_register" in rep        # it saw an empty register
    rep2 = build(res, m, dsr_mode="clustered", register=True)
    assert rep2["n_eff_register"] >= rep2["n_eff"]


def test_verdict_needs_the_two_way_t():
    sel = {"all": {"n": 100, "t_day": 3.0, "t_2way": 1.5}, "is": {"n": 50, "avg_r": 0.1},
           "oos": {"n": 50, "avg_r": 0.1}, "edge_vs_placebo": 0.1, "edge_vs_time": 0.1, "dsr": 0.99, "stress_r": 0.1}
    assert verdict(sel) == "promising"
    assert verdict({**sel, "all": {"n": 100, "t_day": 3.0, "t_2way": 2.5}}) == "SURVIVES"


def test_study_rows_keep_the_legacy_shapes_and_mark_controls():
    rungs = RR.rungs_rows({"eval": {"random": {"n": 9, "targets": 0.1, "targets+d4": {"uplift": -0.01, "t_day": -3}},
                                    "strategies": {"n": 9, "targets+d4": {"uplift": 0.0, "t_day": 0.1}}}}, "rungs")
    assert [(r["params"], r["control"]) for r in rungs] == [({"group": "random", "variant": "targets+d4"}, True),
                                                           ({"group": "strategies", "variant": "targets+d4"}, False)]
    lad = RR.ladder_rows({"vs_hold": {"base": {"n_trades": 3, "mean_delta_R_vs_hold": 0.1},
                                      "placebo": {"n_trades": 3, "mean_delta_R_vs_hold": 0.0}}})
    assert [r["control"] for r in lad] == [False, True] and lad[0]["params"]["variant"] == "base"
    card = RR.card_rows({"funding_settlement_drift": {"two_hours_after_payer_loss": {"n": 5, "mean": -0.1, "t_day": -2}},
                         "intraday_hour_seasonality": {"by_hour": {"16": {"n": 60, "mean_bp": 21.0, "t_day": 3.0}}}})
    assert [r["params"] for r in card] == [{"window": "2h after"}, {"hour": 16}]
    assert card[1]["mean"] == pytest.approx(0.0021)
    hedge = RR.hedge_rows({"strategies": {"pump_fade": {"stats": {"btc_1": {"n": 5, "avg_r": 0.1, "t_day": 1.0}}}}})
    assert hedge[0]["params"] == {"book": "strategies", "strategy": "pump_fade", "variant": "btc_1"}
    trail = RR.trail5m_rows({"pump_fade": {"modes": {"fixed": {"1h": {"delta": None}},
                                                     "be": {"1h": {"delta": {"n": 9, "d": 0.01, "t": 3.4}}}}}})
    assert [r["params"] for r in trail] == [{"exit": "be", "bars": "1h"}]
    pat = RR.patterns_rows({"kinds": {"pump_kk": {"n": 9, "short_ret_24h_mean": 1.0, "t_vs_control": 0.5,
                                                  "by_oi": {"0-5": {"n": 5, "short_ret_24h_mean": 2.0}}}}})
    assert len(pat) == 2 and pat[1]["params"] == {"kind": "pump_kk", "split": "by_oi", "bucket": "0-5"}
    lag = RR.laggard_rows({"labelled_events": 3, "4h": {"events": 30, "days": 9, "t_by_day": 1.2}, "go_rule": "x"})
    assert [r["params"] for r in lag] == [{"h": "4h"}]

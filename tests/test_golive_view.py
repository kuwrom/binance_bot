"""The go-live report's plumbing gate, its text, its brief line and its command (#46 §4, golive_view.py): each drill's
latest pass, each probe's latest result and the backup drill within 30 days, a clean gateway soak; the brief's one
line names the blockers from a light check (no bootstrap); `golive_report` / `shortbot golive` only read; in live mode
a probation line compares the live fills with the model and with demo."""

import json
from datetime import timedelta
from types import SimpleNamespace

from shortbot import cli_brief, cmd_research, golive, golive_view, probes, uptime
from shortbot.commands import build_parser, by_cli, by_name
from shortbot.config import RiskLimits
from shortbot.journal import Journal
from tests import golive_rig as rig
from tests.golive_rig import NOW


def plumb(j):
    return golive_view.plumbing(j, NOW)


def flat_plumbing(age_days=2.0, skip=()):
    j = Journal(":memory:")
    rig.plumbing(j, age_days, skip)
    return j


def test_current_plumbing_passes():
    p = plumb(flat_plumbing())
    assert p["ok"] and p["blockers"] == []
    assert set(p["drills"]) == {"kill-switch", "restart", "backup", "backup-offbox"}
    assert set(p["probes"]) == set(probes.ORDER) and p["backup_drill"]["ok"] and p["soak"]["ok"]


def test_a_drill_pass_is_valid_30_days():
    assert plumb(flat_plumbing(age_days=29.5))["ok"]
    p = plumb(flat_plumbing(age_days=30.5))
    assert not p["ok"] and "drill restart: last pass 30.5 days ago (> 30)" in p["blockers"]


def test_a_missing_or_failed_drill_blocks_and_the_latest_result_wins():
    j = flat_plumbing(skip=("restart",))
    assert "drill restart: never run" in plumb(j)["blockers"]
    rig.decision(j, "drill_result", NOW - timedelta(hours=1), {"drill": "kill-switch", "verdict": "awaiting_clear",
                                                               "ok": False})
    assert "drill kill-switch: latest is awaiting_clear" in plumb(j)["blockers"]
    rig.decision(j, "drill_result", NOW - timedelta(minutes=5), {"drill": "kill-switch", "verdict": "pass", "ok": True})
    assert not any("kill-switch" in b for b in plumb(j)["blockers"])


def test_a_probe_must_match_its_fake_or_be_explained():
    j = flat_plumbing()
    rig.decision(j, "probe_result", NOW - timedelta(days=1), {"probe": "fetch-by-cid", "ok": True, "clean": True,
                                                              "matches_fake": False, "verdict": "found nothing"})
    assert "probe fetch-by-cid: does not match the fake" in plumb(j)["blockers"]
    rig.decision(j, "probe_explained", NOW - timedelta(hours=2), {}, "fetch-by-cid: the fake was fixed in #99")
    p = plumb(j)
    assert p["ok"] and p["probes"]["fetch-by-cid"]["explained"].startswith("fetch-by-cid: the fake was fixed")


def test_an_inconclusive_or_unclean_probe_blocks_but_the_observational_one_does_not():
    j = flat_plumbing()
    assert plumb(j)["probes"]["user-stream"]["ok"]          # matches_fake None by design: it reports what it saw
    rig.decision(j, "probe_result", NOW - timedelta(days=1), {"probe": "adl-quantile", "ok": True, "clean": False,
                                                              "matches_fake": True, "verdict": "v"})
    rig.decision(j, "probe_result", NOW - timedelta(days=1), {"probe": "cancel-finished", "ok": True, "clean": True,
                                                              "matches_fake": None, "verdict": "refused"})
    b = plumb(j)["blockers"]
    assert "probe adl-quantile: left something behind (clean false)" in b
    assert "probe cancel-finished: inconclusive (matches_fake none)" in b


def test_the_backup_drill_kv_must_be_recent():
    j = flat_plumbing()
    j.set("backup_drill_last_ok", json.dumps({"ts": rig.iso(NOW - timedelta(days=31))}))
    assert "backup drill: last pass 31.0 days ago (> 30)" in plumb(j)["blockers"]
    j.db.execute("DELETE FROM kv WHERE k='backup_drill_last_ok'")
    assert "backup drill: no pass recorded (kv backup_drill_last_ok)" in plumb(j)["blockers"]


def intent(j, actor, status, at, iid="I1"):
    j.db.execute("INSERT INTO intents (id, kind, side, args, status, actor, mode, pid, received_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (iid, "close_position", "short", "{}", status, actor, j.mode, 1,
                                                at.isoformat(timespec="milliseconds")))
    j.db.commit()


def test_the_soak_is_clean_without_breakglass_or_unresolved_intents():
    j = flat_plumbing()
    intent(j, "mcp:close_position", "done", NOW - timedelta(days=2))
    intent(j, "breakglass:mcp:close_position", "done", NOW - timedelta(days=40), "OLD")   # outside the 30 days
    assert plumb(j)["soak"]["ok"]
    intent(j, "breakglass:mcp:close_position", "done", NOW - timedelta(days=3), "BG")
    intent(j, "cli:close", "unknown", NOW - timedelta(days=4), "UNK")
    intent(j, "cli:close", "running", NOW - timedelta(days=5), "STUCK")
    s = plumb(j)["soak"]
    assert not s["ok"] and s["breakglass"] == ["BG"] and s["unresolved"] == ["UNK"] and s["stale_open"] == ["STUCK"]


def test_breakglass_inside_a_drill_window_is_explained():
    j = flat_plumbing()
    at = NOW - timedelta(days=3)
    j.set(uptime.WINDOW_KEY + "restart", json.dumps({"from": at.timestamp() - 60, "until": at.timestamp() + 60}))
    intent(j, "breakglass:cli:drill:restart", "done", at, "BG")
    assert plumb(j)["soak"]["ok"]


def test_the_soak_needs_the_gateway_to_have_run():
    j = flat_plumbing()
    j.db.execute("DELETE FROM kv WHERE k='gateway_heartbeat'")
    assert "gateway soak: the gateway never ran (no kv gateway_heartbeat)" in plumb(j)["blockers"]


# ------------------------------------------------------------------ text, brief line, live probation
def test_the_text_lists_each_criterion_and_ends_with_the_owners_steps(monkeypatch):
    rep = golive.report(rig.desk(monkeypatch), RiskLimits(), now=NOW)
    text = golive_view.text(rep)
    assert text.splitlines()[0].startswith("# go-live: READY")
    for name in rep["criteria"]:
        assert f" {name}:" in text
    assert "deploy/DEPLOY.md §6" in text and "minimum size" in text and text.rstrip().endswith(
        golive.OWNER_STEPS[-1])


def test_the_brief_line_names_the_blockers():
    line = golive_view.brief_line(Journal(":memory:"), RiskLimits(), NOW)
    assert line.startswith("- go-live: NOT READY — 9 blockers: registry, sample, expectancy, stress, drawdown, ")
    assert "plumbing" in line and "`shortbot golive`" in line


def test_the_brief_line_is_ready_on_a_ready_desk(monkeypatch):
    line = golive_view.brief_line(rig.desk(monkeypatch), RiskLimits(), NOW)
    assert line.startswith("- go-live: READY") and "owner" in line


def test_the_brief_line_uses_the_light_check(monkeypatch):
    seen = []
    real = golive.report
    monkeypatch.setattr(golive, "report", lambda *a, **k: seen.append(k.get("light")) or real(*a, **k))
    golive_view.brief_line(Journal(":memory:"), RiskLimits(), NOW)
    assert seen == [True]


async def test_the_session_brief_carries_the_line(monkeypatch):
    from tests.test_brief import app
    text = await cli_brief.brief(app())
    assert "- go-live: NOT READY" in text


def test_live_mode_adds_a_probation_line(tmp_path, monkeypatch):
    rig.desk(monkeypatch, path=tmp_path / "j.db").db.close()
    live = Journal(tmp_path / "j.db", "live")
    for i in range(3):
        rig.trade(live, 0.5, NOW - timedelta(days=3 - i), NOW - timedelta(days=3 - i, hours=-2), exit_bps=8.0)
    rep = golive.report(live, RiskLimits(), now=NOW)
    assert rep["mode"] == "live" and rep["verdict"] == "READY"     # the gate reads the demo evidence
    p = rep["live_probation"]
    assert p["n"] == 3 and p["target"] == 30 and p["live_excess_bps"] > p["demo_excess_bps"]
    assert "live probation: 3/30 live trades" in golive_view.text(rep)
    assert "live probation: 3/30" in golive_view.brief_line(live, RiskLimits(), NOW)


# ------------------------------------------------------------------ the command
def test_golive_is_a_read_on_both_surfaces():
    cmd = by_name("golive_report")
    assert cmd.cli == "golive" and not cmd.write and not cmd.orders and by_cli("golive") is cmd
    ns = build_parser().parse_args(["golive", "--text"])
    assert cmd.cli_kwargs(ns) == {"text": True} and "text" not in cmd.mcp_signature().parameters


async def test_golive_report_returns_the_report_or_its_text(monkeypatch):
    j = Journal(":memory:")
    monkeypatch.setattr(cmd_research, "get_app", lambda: SimpleNamespace(
        journal=j, settings=SimpleNamespace(limits=RiskLimits(), mode="demo")))
    rep = await cmd_research.golive_report()
    assert rep["verdict"] == "NOT READY" and "registry" in rep["blockers"]
    assert (await cmd_research.golive_report(text=True)).startswith("# go-live: NOT READY")
    assert j.db.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0


async def test_the_mcp_tool_passes_its_output_schema(monkeypatch):
    from shortbot.mcp_server import mcp
    j = Journal(":memory:")
    monkeypatch.setattr(cmd_research, "get_app", lambda: SimpleNamespace(
        journal=j, settings=SimpleNamespace(limits=RiskLimits(), mode="demo")))
    res = await mcp.call_tool("golive_report", {})
    assert not res.is_error, res
    body = res.structured_content["result"]
    assert body["verdict"] == "NOT READY" and "plumbing" in body["blockers"]
    assert set(body["criteria"]) == set(golive.report(j)["criteria"])

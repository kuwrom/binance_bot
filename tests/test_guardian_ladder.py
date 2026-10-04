"""The guardian's setup-ladder jobs (#69): the daily frozen re-enable check (demo re-enables at HALF, journalled and
alerted as the job; live alerts that a human clear is due, on a cooldown; failing criteria only log; a failure is the
lane's loud failure) and the Telegram leg of the `setup_ladder_stop` rows other processes journal (once each)."""

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from shortbot import guardian
from shortbot import guardian_jobs as gj
from shortbot import setup_ladder as L
from shortbot.config import RiskLimits, Settings
from shortbot.guardian_loop import Job, Lane
from shortbot.journal import Journal
from shortbot.notify import Notifier
from tests.fakes import FakeCcxt
from tests.test_guardian import guardian_with
from tests.test_setup_ladder import GOOD, rows


class Host(gj.MarketJobs):
    """What the ladder jobs use of the Guardian: app.settings, j, n."""

    def __init__(self, mode="demo", telegram=False, path=":memory:"):
        s = Settings(mode, "", "", path, "123:tok" if telegram else "", "42" if telegram else "", RiskLimits())
        self.j = Journal(path, mode)
        self.app = SimpleNamespace(settings=s, journal=self.j)
        self.n = Notifier(s, self.j)


def stopped(h, cands=GOOD):
    """pump_fade hits its ladder stop (14th trade), with `cands` as the shadow evidence since."""
    h.j.closed_results = lambda all_modes=False: rows([2] + [-1] * 13)
    h.j.candidates = lambda labelled_only=False, limit=5000: [
        {"setups": ["pump_fade"], "short_ret_72h": e["ret"], "ts": e["day"] + "T00:00:00"} for e in cands]
    assert L.status(h.j, "pump_fade")["state"] == "SHADOW"


async def run_job(h, name="setup_ladder", fn=None):
    """One pass through a lane, as the guardian runs it (audit.acting, failures alerted)."""
    await Lane("monitor", [Job(name, fn or h.setup_ladder_check, 0, 5)], h.n, None, h.j, 0.01).tick()


def alerts(h, kind):
    return [a for a in h.j.alerts(500) if a["kind"] == kind]


def reenables(h):
    return [d for d in h.j.decisions() if d["action"] == "setup_ladder_reenable"]


# ------------------------------------------------------------ wiring
def test_both_jobs_run_in_the_monitor_lane():
    assert guardian.MONITOR["setup_ladder"][0] == 86400 and guardian.MONITOR["setup_ladder_stops"][0] == 600
    g = guardian_with(FakeCcxt())
    jobs = {j.name: j.fn for j in g._lanes()["monitor"].jobs}
    assert jobs["setup_ladder"] == g.setup_ladder_check and jobs["setup_ladder_stops"] == g.forward_ladder_stops


# ------------------------------------------------------------ daily re-enable check
async def test_demo_reenables_at_half_with_a_decision_and_an_alert_attributed_to_the_job():
    h = Host("demo")
    stopped(h)
    await run_job(h)
    assert L.status(h.j, "pump_fade")["state"] == "HALF"
    d = reenables(h)
    assert len(d) == 1 and d[0]["actor"] == "guardian:setup_ladder" and d[0]["mode"] == "demo"
    snap = json.loads(d[0]["snapshot"])
    assert snap["setup"] == "pump_fade" and snap["evidence"]["n"] == 30 and snap["evidence"]["days"] == 10
    a = alerts(h, "setup_ladder_reenable")
    assert len(a) == 1 and a[0]["actor"] == "guardian:setup_ladder"
    assert "setup pump_fade re-enabled at HALF (probation until +4.3R)" in a[0]["message"]
    assert "30 episodes over 10 days" in a[0]["message"]
    await run_job(h)                                    # not SHADOW any more: nothing new
    assert len(reenables(h)) == 1 and len(alerts(h, "setup_ladder_reenable")) == 1
    assert alerts(h, "guardian_error:setup_ladder") == []


async def test_live_only_alerts_that_a_human_clear_is_due_and_not_every_day():
    h = Host("live")
    stopped(h)
    await run_job(h)
    await run_job(h)                                    # the next day's pass, inside the cooldown
    a = alerts(h, "setup_ladder_clear_due:pump_fade")
    assert len(a) == 1 and "criteria pass for pump_fade" in a[0]["message"]
    assert "shortbot-admin ladder-clear --setup pump_fade --reason" in a[0]["message"]
    assert reenables(h) == [] and L.status(h.j, "pump_fade")["state"] == "SHADOW"
    assert L.events(h.j, "pump_fade")["reenables"] == []
    h.j.set("alert_ts:setup_ladder_clear_due:pump_fade:None", str(time.time() - gj.LADDER_CLEAR_REMIND_S - 1))
    await run_job(h)                                    # still waiting after the cooldown: reminded once more
    assert len(alerts(h, "setup_ladder_clear_due:pump_fade")) == 2


async def test_failing_criteria_only_log(caplog):
    h = Host("demo")
    stopped(h, cands=GOOD[:10])                         # 10 episodes < 30
    before = len(h.j.alerts(500))
    with caplog.at_level(logging.INFO, logger="shortbot.guardian"):
        await run_job(h)
    assert len(h.j.alerts(500)) == before and reenables(h) == []
    assert L.status(h.j, "pump_fade")["state"] == "SHADOW"
    assert any("pump_fade" in r.getMessage() and "SHADOW" in r.getMessage() and r.levelno == logging.INFO
               for r in caplog.records)


async def test_an_exception_fails_the_job_loudly(monkeypatch):
    h = Host("demo")

    def boom(journal, live):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(gj.setup_ladder, "auto_reenable", boom)
    await run_job(h)
    a = alerts(h, "guardian_error:setup_ladder")
    assert len(a) == 1 and "RuntimeError: database is locked" in a[0]["message"]


async def test_a_ladder_that_could_not_be_evaluated_fails_the_job_after_the_others(monkeypatch):
    h = Host("demo")
    stopped(h)
    real = L.auto_reenable
    monkeypatch.setattr(gj.setup_ladder, "auto_reenable", lambda journal, live: [
        {"setup": "bounce_fade", "ok": False, "error": "KeyError: 'at'"}, *real(journal, live)])
    await run_job(h)
    assert len(reenables(h)) == 1                       # pump_fade still re-enabled
    a = alerts(h, "guardian_error:setup_ladder")
    assert len(a) == 1 and "bounce_fade" in a[0]["message"] and "KeyError" in a[0]["message"]


# ------------------------------------------------------------ setup_ladder_stop -> Telegram
def telegram(h, fail=0):
    """Telegram posts of h's notifier: returns the texts delivered; the first `fail` posts raise."""
    sent, left = [], [fail]

    async def post(text):
        if left[0]:
            left[0] -= 1
            raise OSError("telegram down")
        sent.append(text)
    h.n._post = post
    return sent


async def test_a_stop_row_reaches_telegram_exactly_once_and_is_not_journalled_again(tmp_path):
    db = tmp_path / "j.db"
    h, live = Host("demo", telegram=True, path=db), Host("live", path=db)
    sent = telegram(h)
    stopped(h)                                          # the MCP server's sizing saw the stop: a row, no Telegram
    stopped(live)                                       # the live account's stop is not this guardian's
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
    h.j.db.execute("INSERT INTO alerts (ts, kind, message, mode) VALUES (?, 'setup_ladder_stop', 'ancient', 'demo')",
                   (old,))
    h.j.db.commit()
    for _ in range(2):
        await run_job(h, "setup_ladder_stops", h.forward_ladder_stops)
    assert len(sent) == 1 and "pump_fade hit its ladder stop" in sent[0] and "[demo] setup_ladder_stop" in sent[0]
    assert len(alerts(h, "setup_ladder_stop")) == 3     # the two stops and the ancient row: no new row
    assert alerts(h, "guardian_error:setup_ladder_stops") == []


async def test_a_failed_send_is_retried_on_the_next_pass(tmp_path):
    h = Host("demo", telegram=True)
    sent = telegram(h, fail=1)
    stopped(h)
    await h.forward_ladder_stops()
    assert sent == []
    await h.forward_ladder_stops()
    await h.forward_ladder_stops()
    assert len(sent) == 1


async def test_without_telegram_the_row_is_marked_and_not_resent():
    h = Host("demo")
    stopped(h)
    await h.forward_ladder_stops()
    assert h.j.get(gj.KV_STOP_SENT + str(alerts(h, "setup_ladder_stop")[0]["id"])) is not None

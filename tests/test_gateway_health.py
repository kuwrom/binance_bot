"""The gateway's heartbeat (#36 step 4, docs/GATEWAY.md §3.4): kv gateway_heartbeat every 5 s from the gateway; with
SHORTBOT_GATEWAY on, the session brief says up / DOWN / never seen and what still works, and the guardian alerts a
stale one (urgent, once per cooldown). With the flag off neither says anything. It gates nothing: connect and hello
are the liveness check every write makes."""

import time

from shortbot import gateway_health as health
from shortbot.config import Settings
from shortbot.journal import Journal
from tests.fakes import FakeCcxt
from tests.gateway_rig import flag_on

OFF = Settings("demo", "", "", ":memory:", "", "")
ON = flag_on(OFF)


def beat(j, age, running=0):
    health.beat(j, {"at": time.time() - age, "pid": 4242, "code": "abc123def4567890", "v": 1, "running": running})


def test_the_brief_line_says_up_down_or_never_seen_only_with_the_flag_on():
    j = Journal(":memory:")
    assert health.brief_line(j, OFF) is None
    never = health.brief_line(j, ON)
    assert "never seen" in never and "refused" in never and "break-glass" in never
    beat(j, 3, running=2)
    up = health.brief_line(j, ON)
    assert "up" in up and "3 s ago" in up and "2 running" in up and "abc123def456" in up and "DOWN" not in up
    beat(j, health.STALE_S + 30)
    down = health.brief_line(j, ON)
    assert "DOWN" in down and "refused" in down and "break-glass" in down
    assert health.brief_line(j, OFF) is None


async def test_the_session_brief_carries_the_line_only_with_the_flag_on():
    from shortbot.cli_brief import brief
    from tests.test_brief import app
    a = app()
    assert not [ln for ln in (await brief(a)).splitlines() if "gateway" in ln.lower()]
    a.settings = flag_on(a.settings)
    (line,) = [ln for ln in (await brief(a)).splitlines() if "order gateway" in ln]
    assert "never seen" in line


async def test_the_guardian_alerts_a_stale_gateway_once_per_cooldown_only_with_the_flag_on():
    from shortbot.guardian import MONITOR
    from tests.test_guardian import guardian_with
    g = guardian_with(FakeCcxt())
    assert MONITOR["gateway"] == (30, 30) and any(job.name == "gateway" for job in g._lanes()["monitor"].jobs)

    def down():
        return [a for a in g.j.alerts() if a["kind"] == "gateway_down"]
    await g.check_gateway()
    assert down() == []                                   # the flag is off: nobody uses it
    g.app.settings = flag_on(g.app.settings)
    beat(g.j, 2)
    await g.check_gateway()
    assert down() == []                                   # fresh
    beat(g.j, health.STALE_S + 5)
    await g.check_gateway()
    await g.check_gateway()
    (a,) = down()                                         # once per cooldown
    assert "refused" in a["message"] and "break-glass" in a["message"] and "docker compose" in a["message"]

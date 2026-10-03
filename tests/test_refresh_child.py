"""#80: the weekly refresh child never outlives the guardian. It starts as the leader of its own process group, so the
histdata and backtest steps it runs join that group; a guardian that is cancelled or stopped (SIGTERM) ends the whole
group and re-arms the refresh to retry in RETRY_S, as a failed run does."""

import asyncio
import os
import signal
import sys
import time

import pytest

from shortbot import guardian as gmod
from shortbot import refresh
from tests.test_guardian_loop import fast_guardian, noop
from tests.test_stream_task import until

# stands in for `python -m shortbot.refresh`: one long step in a grandchild, like refresh's histdata build
REFRESH = """
import os, subprocess, sys, time
step = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(sys.argv[1] + ".tmp", "w") as f:
    f.write(f"{os.getpid()} {step.pid}")
os.rename(sys.argv[1] + ".tmp", sys.argv[1])
time.sleep(60)
"""


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:   # macOS: an exited, unreaped child in another session answers EPERM; it no longer runs
        return False
    return True


def _fake_refresh(monkeypatch, tmp_path):
    pids = tmp_path / "pids"
    monkeypatch.setattr(refresh, "CMD", (sys.executable, "-c", REFRESH, str(pids)))
    return pids


async def _started(pids):
    await until(pids.exists, timeout=10)
    leader, step = map(int, pids.read_text().split())
    assert os.getpgid(step) == os.getpgid(leader) == leader != os.getpgid(0)   # its own group, the step inside it
    return leader, step


async def test_a_cancelled_guardian_leaves_no_refresh_process_and_retries_it(monkeypatch, tmp_path):
    pids = _fake_refresh(monkeypatch, tmp_path)
    g = fast_guardian(monkeypatch)
    del g.research_refresh                          # the real job: due on a fresh journal, so it spawns at once
    task = asyncio.create_task(g.run())
    leader, step = await _started(pids)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await until(lambda: not alive(leader) and not alive(step), timeout=10)
    assert g._refresh_proc is None
    now = time.time()
    assert not refresh.due(g.j, now) and refresh.due(g.j, now + refresh.RETRY_S + 1)   # cut short: retried in 6h


async def test_a_finished_refresh_is_left_alone(monkeypatch, tmp_path):
    """It wrote its own next run; its pid may belong to another process by now, so nothing is signalled."""
    monkeypatch.setattr(refresh, "CMD", (sys.executable, "-c", "pass"))
    proc = await refresh.spawn()
    await proc.wait()
    sent = []
    monkeypatch.setattr(refresh.os, "killpg", lambda *a: sent.append(a))
    assert await refresh.stop(proc) is False and await refresh.stop(None) is False and sent == []


async def test_a_refresh_that_exits_while_being_stopped_is_not_reported_running(monkeypatch):
    """macOS answers killpg on a group whose members have all exited but are not yet reaped with EPERM, not ESRCH:
    a refresh that finishes as the guardian stops must count as gone, not as 'may still be running'."""
    monkeypatch.setattr(refresh, "CMD", (sys.executable, "-c", "import time; time.sleep(60)"))
    proc = await refresh.spawn()

    def killpg(pgid, sig):
        os.kill(proc.pid, signal.SIGKILL)       # it exits on its own just as we signal its group ...
        raise PermissionError(1, "Operation not permitted")   # ... and the kernel reports EPERM for the zombie group
    monkeypatch.setattr(refresh.os, "killpg", killpg)
    assert await refresh.stop(proc, grace_s=2) is True    # no exception: it was running, it is gone now
    assert proc.returncode is not None


async def test_sigterm_stops_the_guardian_through_its_shutdown(monkeypatch):
    """`kill` / `docker stop` send SIGTERM: the guardian cancels itself, so its shutdown ends the refresh group."""
    loop, handlers, shut = asyncio.get_running_loop(), {}, []
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb, *a: handlers.update({sig: (cb, a)}))

    class Guardian:
        async def run(self):
            try:
                await asyncio.Event().wait()
            finally:
                shut.append("shutdown")
    monkeypatch.setattr(gmod, "Guardian", Guardian)
    monkeypatch.setattr(gmod, "close_app", noop)
    task = asyncio.create_task(gmod._main())
    await until(lambda: signal.SIGTERM in handlers)
    cb, args = handlers[signal.SIGTERM]
    cb(*args)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert shut == ["shutdown"]


def test_main_exits_as_a_terminated_process_after_a_clean_sigterm_shutdown(monkeypatch):
    async def stopped():
        raise asyncio.CancelledError
    monkeypatch.setattr(gmod, "_main", stopped)
    monkeypatch.setattr(gmod, "install_log_redaction", lambda: None)
    monkeypatch.setattr(gmod.audit, "set_process", lambda *a, **k: None)
    monkeypatch.setattr(gmod.logging, "basicConfig", lambda **k: None)
    with pytest.raises(SystemExit) as e:
        gmod.main()
    assert e.value.code == 128 + signal.SIGTERM

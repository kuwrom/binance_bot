import pytest


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    async def instant(_s):
        return None
    monkeypatch.setattr("shortbot.trading._sleep", instant)


@pytest.fixture(autouse=True)
def warm_desk(request, monkeypatch):
    """Order-flow tests assume a warm desk; test_warmup.py exercises the real gate."""
    if "test_warmup" not in request.node.nodeid:
        monkeypatch.setattr("shortbot.warmup.gate", lambda j, now=None: None)


@pytest.fixture(autouse=True)
def guardian_alive(request, monkeypatch):
    """Order-flow tests assume a live guardian; test_killswitch*.py exercise the real heartbeat watchdog."""
    if "test_killswitch" not in request.node.nodeid:
        async def alive(tr, now=None):
            return None
        monkeypatch.setattr("shortbot.killswitch.watch_guardian", alive)

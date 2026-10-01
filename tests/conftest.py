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

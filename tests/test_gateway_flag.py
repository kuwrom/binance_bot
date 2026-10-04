"""SHORTBOT_GATEWAY (#36 steps 5-6): on by default for the sessions and the guardian; `sessions` keeps the guardian
in-process (step 6's rollback); 0/off keeps everything in-process (step 5's rollback). A value it doesn't understand
is the default, logged and alerted (config_invalid): it never stops a process from starting."""

import logging
from dataclasses import replace

import pytest

from shortbot import procs
from shortbot.config import Settings, gateway_flag
from tests.fakes import FakeCcxt
from tests.test_guardian import guardian_with


@pytest.mark.parametrize("raw, want", [
    (None, (True, True)), ("", (True, True)), ("1", (True, True)), (" On ", (True, True)), ("yes", (True, True)),
    ("sessions", (True, False)), ("SESSIONS", (True, False)),
    ("0", (False, False)), ("off", (False, False)), ("false", (False, False)), ("no", (False, False))])
def test_values(raw, want):
    assert gateway_flag(raw) == (*want, None)


def test_the_default_is_on(monkeypatch):
    monkeypatch.delenv("SHORTBOT_GATEWAY", raising=False)
    s = Settings.from_env()
    assert (s.gateway, s.gateway_guardian, s.invalid) == (True, True, ())
    monkeypatch.setenv("SHORTBOT_GATEWAY", "0")
    s = Settings.from_env()
    assert (s.gateway, s.gateway_guardian) == (False, False)


def test_an_unknown_value_is_the_default_and_logged(monkeypatch, caplog):
    monkeypatch.setenv("SHORTBOT_GATEWAY", "maybe")
    with caplog.at_level(logging.WARNING, "shortbot.config"):
        s = Settings.from_env()   # never raises: a typo must not stop the guardian
    assert (s.gateway, s.gateway_guardian) == (True, True)
    assert len(s.invalid) == 1 and "SHORTBOT_GATEWAY='maybe'" in s.invalid[0]
    assert "SHORTBOT_GATEWAY='maybe'" in caplog.text


async def test_the_guardian_starts_and_alerts_an_invalid_value(monkeypatch):
    g = guardian_with(FakeCcxt())
    monkeypatch.setattr(procs, "docker_vm", lambda data_dir: None)
    _, _, why = gateway_flag("maybe")
    g.app.settings = replace(g.app.settings, invalid=(why,))
    sent = []
    real = g.n.send

    async def send(kind, msg, *a, **k):
        sent.append((kind, k.get("urgent")))
        return await real(kind, msg, *a, **k)
    monkeypatch.setattr(g.n, "send", send)
    await g._startup()
    assert ("config_invalid", True) in sent
    [a] = [a for a in g.j.alerts() if a["kind"] == "config_invalid"]
    assert "SHORTBOT_GATEWAY='maybe'" in a["message"] and ".env" in a["message"]

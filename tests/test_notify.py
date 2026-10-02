"""Alert plumbing (#23): secrets never reach a row, a log line or Telegram; a failed send is armed on attempt and
retried after a window; a send is time-boxed; session alerts are all journalled and only urgent ones go out."""

import asyncio
import json
import logging
import time
from types import SimpleNamespace

import httpx

from shortbot import app as app_mod
from shortbot import notify
from shortbot.config import RiskLimits, Settings
from shortbot.journal import Journal
from shortbot.notify import Notifier
from tests.fakes import SYM

TOKEN = "123456:SECRET-tok_en"
TG = Settings("demo", "", "", ":memory:", TOKEN, "42", RiskLimits())
SIGNED = ("binanceusdm GET https://fapi.binance.com/fapi/v2/positionRisk?timestamp=1759400000000&recvWindow=10000"
          "&signature=9f2c0ab1e4d7 RequestTimeout")
_RealClient = httpx.AsyncClient


def telegram(monkeypatch, reply):
    """Every Notifier post goes to `reply(request)` (sync or async); returns the JSON bodies sent."""
    sent = []

    async def handler(request):
        sent.append(json.loads(request.content))
        r = reply(request)
        return await r if asyncio.iscoroutine(r) else r
    monkeypatch.setattr(notify.httpx, "AsyncClient",
                        lambda **kw: _RealClient(transport=httpx.MockTransport(handler), **kw))
    return sent


def ok(_request):
    return httpx.Response(200, json={"ok": True})


def clock(monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(notify, "time", SimpleNamespace(time=lambda: now[0]))
    return now


async def test_a_failed_send_is_armed_on_attempt_logged_without_the_token_and_retried_after_the_window(
        monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    sent = telegram(monkeypatch, lambda r: httpx.Response(401, json={"ok": False}))   # a revoked token
    now = clock(monkeypatch)
    j = Journal(":memory:")
    n = Notifier(TG, j)
    assert not await n.send("stop_near", "m", SYM, cooldown_s=4 * 3600)
    assert not await n.send("stop_near", "m", SYM, cooldown_s=4 * 3600)   # next pass: no re-send, no new row
    assert len(sent) == 1 and len(j.alerts()) == 1
    assert "SECRET" not in caplog.text and "bot<redacted>" in caplog.text
    now[0] += notify.TELEGRAM_RETRY_S + 1
    await n.send("stop_near", "m", SYM, cooldown_s=4 * 3600)
    assert len(sent) == 2                     # retried: a Telegram blip doesn't swallow it for the full cooldown


async def test_a_delivered_send_keeps_the_full_cooldown(monkeypatch):
    sent = telegram(monkeypatch, ok)
    now = clock(monkeypatch)
    n = Notifier(TG, Journal(":memory:"))
    assert await n.send("stop_near", "m", SYM, cooldown_s=4 * 3600)
    now[0] += notify.TELEGRAM_RETRY_S + 1
    assert not await n.send("stop_near", "m", SYM, cooldown_s=4 * 3600)
    assert len(sent) == 1


async def test_a_hung_telegram_send_is_time_boxed(monkeypatch):
    async def hang(_request):
        await asyncio.sleep(30)
    telegram(monkeypatch, hang)
    monkeypatch.setattr(notify, "TELEGRAM_TIMEOUT_S", 0.05)
    j = Journal(":memory:")
    t0 = time.monotonic()
    assert not await Notifier(TG, j).send("NAKED_POSITION", "m", SYM, cooldown_s=0, urgent=True)
    assert time.monotonic() - t0 < 2 and len(j.alerts()) == 1   # the row is written before the send


async def test_signed_urls_and_bot_tokens_never_reach_a_row_a_log_line_or_telegram(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    sent = telegram(monkeypatch, ok)
    j = Journal(":memory:")
    await Notifier(TG, j).send("position_check_error", f"{SIGNED} https://api.telegram.org/bot{TOKEN}/x", SYM,
                               cooldown_s=0)
    j.log_alert("stop_restored", SIGNED, SYM)   # direct writers: Trader's fallback, reconcile, ensure_stop
    texts = [a["message"] for a in j.alerts()] + [sent[0]["text"], caplog.text]
    for secret in ("9f2c0ab1e4d7", "1759400000000", "SECRET"):
        assert not any(secret in t for t in texts), secret
    assert all("signature=<redacted>" in a["message"] for a in j.alerts())
    assert "recvWindow=10000" in j.alerts()[0]["message"]   # only the secrets go


async def test_session_trader_journals_every_alert_and_sends_only_urgent_ones(monkeypatch):
    sent = telegram(monkeypatch, ok)
    monkeypatch.setattr(app_mod.Settings, "from_env", lambda: TG)
    monkeypatch.setattr(app_mod, "Exchanges", lambda s: SimpleNamespace(params=None))
    monkeypatch.setattr(app_mod, "_app", None)
    tr = app_mod.get_app().trader
    for _ in range(2):
        await tr.alert("action_fill", "filled", SYM)   # the audit trail: never dropped by a cooldown
    for _ in range(2):
        await tr.alert("NAKED_POSITION", "no stop", SYM, True)
    assert [a["kind"] for a in tr.j.alerts()] == ["NAKED_POSITION"] * 2 + ["action_fill"] * 2
    assert len(sent) == 2 and all("NAKED_POSITION" in s["text"] for s in sent)

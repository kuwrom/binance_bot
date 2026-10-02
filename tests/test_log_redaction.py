"""#66: a logged ccxt error carries the signed request URL, in the message (log.warning("%s", e)) and in a traceback
(log.exception). Every entry point installs a filter that redacts each record before a handler writes it."""

import io
import logging
import sys

import pytest

from shortbot.redact import RedactFilter, install_log_redaction

SIGNED = ("binanceusdm GET https://fapi.binance.com/fapi/v1/openOrders?timestamp=1759400000000&recvWindow=10000"
          "&signature=9f2c0ab1e4d7 RequestTimeout")


def capture(*handlers):
    log = logging.getLogger("shortbot.test_redaction")
    log.handlers, log.propagate = list(handlers), False
    return log


def stream():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    h.addFilter(RedactFilter())
    return h, buf


def test_a_logged_error_and_its_traceback_are_redacted_for_every_handler():
    (a, buf_a), (b, buf_b) = stream(), stream()
    log = capture(a, b)
    try:
        raise RuntimeError(SIGNED)
    except RuntimeError as e:
        log.warning("poll failed: %s", e)
        log.exception("entry gate sweep failed")
    for out in (buf_a.getvalue(), buf_b.getvalue()):
        assert "9f2c0ab1e4d7" not in out and "1759400000000" not in out, out
        assert out.count("signature=<redacted>") == 2 and "recvWindow=10000" in out   # only the secrets go
        assert "Traceback (most recent call last)" in out and "RuntimeError" in out and "poll failed" in out


def test_install_covers_every_root_handler_and_the_last_resort_once(monkeypatch):
    root, last = logging.StreamHandler(io.StringIO()), logging.StreamHandler(io.StringIO())
    monkeypatch.setattr(logging.root, "handlers", [root])
    monkeypatch.setattr(logging, "lastResort", last)
    install_log_redaction()
    install_log_redaction()
    assert [type(f) for f in root.filters] == [RedactFilter] and [type(f) for f in last.filters] == [RedactFilter]


@pytest.mark.parametrize("entry", ["guardian", "mcp_server", "cli", "admin"])
def test_every_entry_point_installs_it(monkeypatch, entry):
    import importlib

    from shortbot import audit
    mod = importlib.import_module(f"shortbot.{entry}")
    called = []
    monkeypatch.setattr(mod, "install_log_redaction", lambda: called.append(entry))
    monkeypatch.setattr(audit, "set_process", lambda *a, **k: None)

    async def done(*a, **k):
        return {"ok": True}
    if entry == "guardian":
        monkeypatch.setattr(mod, "_main", done)
    elif entry == "mcp_server":
        monkeypatch.setattr(mod.mcp, "run", lambda *a, **k: None)
    elif entry == "cli":
        monkeypatch.setattr(sys, "argv", ["shortbot", "clock"])
        monkeypatch.setattr(mod, "_audited", done)
    else:
        monkeypatch.setattr(sys, "argv", ["shortbot-admin", "status"])
        monkeypatch.setattr(mod, "run", done)
    mod.main()
    assert called == [entry]

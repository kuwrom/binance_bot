"""Secrets that error texts carry into alerts: ccxt puts a signed request's full URL in its exception text (GET and
DELETE calls carry `timestamp=` and `signature=` in the query), and httpx puts the Telegram URL, bot token included,
in its HTTP errors. Every alert row (Journal.log_alert), alert log line and Telegram message goes through here, and
every log record of a process whose entry point called install_log_redaction (guardian, MCP server, CLIs)."""

from __future__ import annotations

import logging
import re

_BOT_TOKEN = re.compile(r"bot\d+:[\w-]+")
_SIGNED = re.compile(r"\b(signature|timestamp)=[^&\s'\"]+")


def redact(text: str) -> str:
    return _SIGNED.sub(r"\1=<redacted>", _BOT_TOKEN.sub("bot<redacted>", str(text)))


class RedactFilter(logging.Filter):
    """Redacts a record's message and traceback before any handler writes it: a ccxt error logged with
    log.exception or log.warning("%s", e) carries the signed URL (#66). The traceback is rendered to text here, so no
    handler re-renders it from the raw exception."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), None
        if record.exc_info:
            record.exc_text = record.exc_text or logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def install_log_redaction() -> None:
    """Every handler of the root logger, and the last-resort one a process without logging config writes through.
    Idempotent. Call after the logging config (basicConfig) is set up."""
    for h in [*logging.root.handlers, logging.lastResort]:
        if h is not None and not any(isinstance(f, RedactFilter) for f in h.filters):
            h.addFilter(RedactFilter())

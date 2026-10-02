"""Secrets that error texts carry into alerts: ccxt puts a signed request's full URL in its exception text (GET and
DELETE calls carry `timestamp=` and `signature=` in the query), and httpx puts the Telegram URL, bot token included,
in its HTTP errors. Every alert row (Journal.log_alert), alert log line and Telegram message goes through here."""

from __future__ import annotations

import re

_BOT_TOKEN = re.compile(r"bot\d+:[\w-]+")
_SIGNED = re.compile(r"\b(signature|timestamp)=[^&\s'\"]+")


def redact(text: str) -> str:
    return _SIGNED.sub(r"\1=<redacted>", _BOT_TOKEN.sub("bot<redacted>", str(text)))

"""Third-party text (RSS headlines, Binance announcement titles, social summaries, calendar titles) is DATA for the
agent, never instructions. It reaches the agent only inside tool results, wrapped as {"untrusted_text", "source"} by
`wrap`, and never in the SessionStart brief (hook output is not a tool result). Delimiters alone stop only part of
prompt injection, so `clean` also removes the usual carriers: invisible characters (zero-width, bidi, Unicode tags),
chat/role markup and line breaks that fake a new block. Our own text (alert messages, symbols) only gets `flat`."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_MARKUP = re.compile(
    r"<\|[^<>|]{0,64}\|>"                                  # chat-template tokens: <|im_start|>
    r"|\[/?(?:INST|SYS)\]|<</?SYS>>"                       # [INST], <<SYS>>
    r"|</?[A-Za-z][\w:.-]{0,40}(?:\s[^<>]{0,200})?/?>"     # xml/html tags: <system>, </function_calls>, <img ...>
    r"|`{3,}", re.IGNORECASE)                              # code fences
_ROLE = re.compile(r"\b(system|assistant|human|user|developer)\s*:", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\"'`{}|\\^]{1,500}")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z _-]{0,19}")
# Alert kinds whose message carries third-party text: the guardian's news_on_position (headline) and binance_*
# (announcement title). The brief never prints their message; tools wrap it.
THIRD_PARTY_ALERTS = ("news_on_position", "binance_")
# Learning provenance tags that mean "the evidence came from third-party text" (see add_learning).
THIRD_PARTY_SOURCES = ("coin_news", "latest_announcements", "coin_snapshot.news", "news", "announcement", "rss",
                       "social", "web", "url")


def _visible(text: str) -> str:
    """Control and line/paragraph separators become spaces; invisible format characters (Cf) are dropped."""
    return "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c
                   for c in text if unicodedata.category(c) != "Cf")


def flat(text: Any, limit: int = 140) -> str:
    """One line of OUR OWN text (alert messages, exception strings): no line breaks or control characters, capped."""
    t = re.sub(r"\s+", " ", _visible(str(text or ""))).strip()
    return t if len(t) <= limit else t[:limit - 1] + "…"


def clean(text: Any, limit: int = 300) -> str:
    """Third-party text, normalised (NFKC, so a fullwidth ＜system＞ is caught too), invisible characters and
    markup removed, role prefixes defused ("SYSTEM:" -> "SYSTEM -"), one line, capped."""
    t = _visible(unicodedata.normalize("NFKC", str(text or "")))
    return flat(_ROLE.sub(r"\1 -", _MARKUP.sub(" ", t)), limit)


def safe_url(url: Any) -> str:
    """A plain http(s) URL, or '' (no spaces, quotes, angle brackets or invisible characters)."""
    u = str(url or "").strip()
    return u if u.isprintable() and _URL.fullmatch(u) else ""


def token(value: Any) -> str | None:
    """A short third-party label (e.g. a sentiment 'Positive'), or None when it is anything more than a label."""
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def wrap(text: Any, source: str, limit: int = 300) -> dict[str, str]:
    """The only shape in which third-party text is returned to the agent."""
    return {"untrusted_text": clean(text, limit), "source": source}


def third_party_alert(kind: str) -> bool:
    return kind.startswith(THIRD_PARTY_ALERTS)


def third_party_tag(tag: str) -> bool:
    return str(tag).lower().startswith(THIRD_PARTY_SOURCES)

"""Market-wide news, shadow only (#92; docs/MARKET_MAP.md "Market news"): BTC and ETH headlines from the existing
sources (news.news_for: Binance announcements and the RSS feeds, cleaned there as untrusted text) are tagged by a small
keyword classifier for the classes that moved the whole market on 2026-10-08: government or large-holder transfers,
ETF flows, exchange hacks or insolvency, war or geopolitics, regulation or enforcement.

Each new tagged item (deduped by URL and by normalised title) is alerted once to both desks (the market guardian runs
this job; alerts_view.MARKET_ACTORS) and logged in `market_news_events` with the reference prices of BTC, ETH and the
ALT_N most liquid alts, so `market_news_label.label` can fill their next 1h/4h/24h once due. Context, never a trigger:
nothing here reads or changes orders, gates or sizes until forward data says the tags predict anything.

The alert's first line is our own text (classes, coin, source, time); the headline and link follow it and are
third-party (untrusted.THIRD_PARTY_ALERTS): the brief prints only the first line, tools wrap the rest."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from . import news

log = logging.getLogger("shortbot.market_news")
COINS = ("BTC", "ETH")
MAX_ITEMS = 40            # per coin and source: news_for's cut, after its own 48h RSS window
FRESH_S = 3 * 3600        # older items are marked seen, never alerted or logged (a late anchor would bias the labels)
BURST_MAX = 3             # urgent alerts per rolling hour; the rest of a burst are KIND_MORE (routine)
ALT_N = 20                # the alt basket: the most liquid coin perps by 24h quote volume, BTC and ETH excluded
KIND, KIND_MORE = "market_news", "market_news_more"
KV_SEEN, KV_INIT, KV_MORE_TG = "mnews_seen:", "market_news_initialised", "market_news_more_telegram"
MORE_TELEGRAM_S = 3600    # market_news_more: one Telegram message an hour; every item still gets its alert row
MAJORS = ("BTC/USDT:USDT", "ETH/USDT:USDT")

_I = re.IGNORECASE
_USD = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?(k|m|mn|b|bn|million|billion)?\b", _I)
_SCALE = {"k": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
# phrases that only look like conflict: struck out before matching
_NOT_WAR = re.compile(r"\b(?:price|fee|fees|bidding|culture|console|flippening|l2)\s+wars?\b|\bwar\s+chest\b|"
                      r"\b(?:strik(?:e|es|ing)|struck)\s+(?:an?\s+|the\s+)?(?:\w+\s+)?"
                      r"(?:deal|agreement|partnership|pact)\b", _I)
_SCHEDULED = re.compile(r"\b(?:maintenance|upgrade|hard fork|scheduled|network update)\b", _I)

# class: alternatives; one matches when every pattern in it matches
_HOLDER = (r"\b(?:governments?|govt?|seized|confiscated|doj|department of justice|marshals?|mt\.?\s?gox|silk road|"
           r"whales?|satoshi[- ]era|dormant|long[- ]term (?:\w+ )?holders?|lths?|miners?|foundation|bhutan\w*|"
           r"el salvador)\b")
_MOVE = (r"\b(?:mov(?:e|es|ed|ing)|transfer(?:s|red|ring)?|sen(?:d|ds|t|ding)|deposit(?:s|ed|ing)?|offload\w*|"
         r"dump(?:s|ed|ing)?|sell(?:s|ing|ers?)?|sold|liquidat\w*|wak(?:e|es|ing)|woke)\b")
_ETF = r"\betfs?\b"
_FLOW = r"\b(?:out|in)flows?\b|\bflows?\b|\bredemptions?\b|\bbleed\w*|\bexodus\b"
_EXTREME = r"\b(?:record|largest|biggest|heaviest|massive|worst|unprecedented|streak|exodus)\b"
ETF_USD_MIN = 500e6   # a flow without an extreme word counts from this size (routine daily reports are smaller)
_HACK = (r"\b(?:hack(?:s|ed|er|ers)?|exploit(?:s|ed)?|breach(?:es|ed)?|drain(?:s|ed)?|stolen|heist|insolven\w*|"
         r"bankrupt\w*|chapter 11|exit scam|depeg\w*)\b")
_VENUE = (r"\b(?:exchanges?|cex|binance|coinbase|okx|bybit|kraken|bitfinex|bitget|kucoin|gate\.io|htx|upbit|bithumb|"
          r"robinhood|custod\w*|bridge|stablecoin|tether|usdt|usdc)\b")
_HALT = (r"\b(?:halt|suspend|paus|freez|froze|stop)\w*\s+(?:all\s+|\w+\s+and\s+|\w+\s+)?withdrawals?\b|"
         r"\bwithdrawals?\s+(?:halted|suspended|paused|frozen|stopped)\b")
_NATION = (r"\b(?:iran\w*|israel\w*|russia\w*|ukrain\w*|taiwan\w*|north korea\w*|venezuela\w*|china|chinese|"
           r"middle east|gaza|hamas|hezbollah|houthis?|red sea|syria\w*|yemen\w*|pakistan\w*|india|nato|pentagon|"
           r"trade war|world war)\b")
_CONFLICT = (r"\b(?:wars?|warfare|strikes?|struck|air\s?strikes?|military|missiles?|troops|invasion|invad\w*|"
             r"attack\w*|bomb\w*|retaliat\w*|escalat\w*|tensions?|conflict\w*|ceasefire|sanctions?|tariffs?|"
             r"nuclear|martial law|coup)\b")
_AGENCY = (r"\b(?:sec|cftc|doj|department of justice|fbi|ofac|irs|u\.?s\.? treasury|treasury department|"
           r"treasury secretary|finra|fca|esma|regulators?|court|judge|congress|senate|white house|lawmakers|"
           r"government|ministry|central bank|pboc|attorney general|prosecutors?)\b")
_ACTION = (r"\b(?:sues?|sued|lawsuit|charge[sd]?|indict\w*|arrest\w*|crackdown|ban(?:s|ned|ning)?|enforcement|"
           r"subpoena\w*|fine[sd]?|penalt\w*|probe[sd]?|investigat\w*|sanction\w*|reject\w*|den(?:y|ies|ied)|"
           r"approv\w*|delay\w*|raid\w*|shut\s?down|bill|act|ruling)\b")
CLASSES: dict[str, tuple[tuple[str, ...], ...]] = {
    "holder_transfer": ((_HOLDER, _MOVE),),
    "etf_flow": ((_ETF, _FLOW, _EXTREME),),            # or ETF + flow + ETF_USD_MIN (classify)
    "hack_insolvency": ((_HACK, _VENUE), (_HALT,)),    # or a hack of HACK_USD_MIN+ (classify); no scheduled halts
    "geopolitics": ((_NATION, _CONFLICT),),
    "regulation": ((_AGENCY, _ACTION), (r"\bexecutive orders?\b",)),
}
_COMPILED = {c: tuple(tuple(re.compile(p, _I) for p in alt) for alt in alts) for c, alts in CLASSES.items()}
_P = {k: re.compile(v, _I) for k, v in (("etf", _ETF), ("flow", _FLOW), ("hack", _HACK))}
HACK_USD_MIN = 50e6   # a hack with no venue named counts when it names at least this much


def usd_amount(text: str) -> float:
    """The largest dollar amount a headline names ('$1B', '$770 million', '$1.2bn'), 0 when none."""
    best = 0.0
    for m in _USD.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        best = max(best, v * _SCALE.get((m.group(2) or "").lower(), 1.0))
    return best


def classify(title: str) -> list[str]:
    """The market-moving classes a headline names, sorted; [] for everything else. Title only: summaries add noise."""
    text = _NOT_WAR.sub(" ", title or "")
    out = {c for c, alts in _COMPILED.items() if any(all(p.search(text) for p in alt) for alt in alts)}
    usd = usd_amount(text)
    if _P["etf"].search(text) and _P["flow"].search(text) and usd >= ETF_USD_MIN:
        out.add("etf_flow")
    if _P["hack"].search(text) and usd >= HACK_USD_MIN:
        out.add("hack_insolvency")
    if "hack_insolvency" in out and _SCHEDULED.search(text) and not _P["hack"].search(text):
        out.discard("hack_insolvency")   # a withdrawal pause for maintenance or an upgrade is routine
    return sorted(out)


def norm_title(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (title or "").lower()))


def item_keys(item: dict) -> list[str]:
    """Dedupe keys: the link and the normalised title, hashed. The same story under a second link, or a re-posted
    link, is the same item."""
    keys = [f"u{_h(item['url'])}"] if item.get("url") else []
    t = norm_title(item.get("title", ""))
    return keys + ([f"t{_h(t)}"] if t else [])


def _h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def _fresh(item: dict, now: float) -> bool:
    try:
        ts = datetime.fromisoformat(item["time"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return False
    return now - FRESH_S <= ts <= now + 600


async def poll(ex, journal, notifier, now: float | None = None) -> dict[str, Any]:
    """One pass: fetch, tag, dedupe, then per new fresh item: alert it, log it, and only then mark it seen. A failure on
    the way leaves the item for the next pass: a repeated alert, never a lost one (the row's unique key logs it once).
    The first pass ever only marks what is already there (no alert flood on a deploy). Raises when every news source
    failed."""
    now = time.time() if now is None else now
    res = await asyncio.gather(*(news.news_for(c, max_items=MAX_ITEMS) for c in COINS))
    errors = sorted({e for r in res for e in r.get("errors") or []})
    if len(errors) >= 2:   # binance_announcements and rss both down: no news is NOT "no market news"
        raise RuntimeError(f"market news: every source failed ({', '.join(errors)})")
    first = journal.get(KV_INIT) is None
    new: list[tuple[str, dict, list[str], list[str]]] = []
    taken: set[str] = set()   # this pass: the same story on BTC's and ETH's lists is one item
    for coin, r in zip(COINS, res, strict=True):
        for it in (r.get("binance_announcements") or []) + (r.get("headlines") or []):
            classes, keys = classify(it.get("title", "")), item_keys(it)
            if not classes or not keys or taken & set(keys) or any(journal.get(KV_SEEN + k) for k in keys):
                continue
            taken |= set(keys)
            if first or not _fresh(it, now):
                _mark(journal, keys, now)   # never alerted: a late anchor would bias the labels
            else:
                new.append((coin, it, classes, keys))
    journal.mark(KV_INIT, "1")
    if not new:
        return {"new": 0, "errors": errors, "first_run": first}
    ref, ctx = await _reference(ex), _context(journal)
    sent, n = _sent_last_hour(journal, now), 0
    for coin, it, classes, keys in new:
        await _alert(journal, notifier, message(coin, it, classes), urgent=sent < BURST_MAX, now=now)
        sent, n = sent + 1, n + 1
        row = (datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"), int(now * 1000), it.get("time"),
               news.source_of(it), coin, ",".join(classes), it.get("title", ""), it.get("url", ""), keys[0],
               json.dumps(ref) if ref else None, json.dumps(ctx) if ctx else None)
        await journal.write(lambda j, row=row: j.db.execute(
            "INSERT OR IGNORE INTO market_news_events (ts, ts_ms, published, source, coin, classes, title, url, key, "
            "ref, context) VALUES (?,?,?,?,?,?,?,?,?,?,?)", row))
        _mark(journal, keys, now)
    return {"new": n, "errors": errors, "first_run": False}


async def _alert(journal, notifier, msg: str, urgent: bool, now: float) -> None:
    """Urgent (Telegram) for the first BURST_MAX an hour; the rest of a burst are routine market_news_more rows, of
    which Telegram gets one an hour (MORE_TELEGRAM_S)."""
    if urgent:
        await notifier.send(KIND, msg, None, cooldown_s=0, urgent=True)
        return
    last = journal.get(KV_MORE_TG)
    if last and now - float(last) < MORE_TELEGRAM_S:
        journal.log_alert(KIND_MORE, msg)   # the row only: both sessions still see it
        return
    journal.mark(KV_MORE_TG, str(now))
    await notifier.send(KIND_MORE, msg, None, cooldown_s=0)


def _mark(journal, keys: list[str], now: float) -> None:
    for k in keys:
        journal.mark(KV_SEEN + k, str(now))


def message(coin: str, item: dict, classes: list[str]) -> str:
    """First line ours (classes, coin, source, time); then the third-party headline and link."""
    head = (f"market-moving news? [{', '.join(classes)}] {coin} headline, {news.source_of(item)}, published "
            f"{str(item.get('time', ''))[11:16]} UTC. Shadow context, not a trigger: check market_map and positions")
    return f"{head}\n{item.get('title', '')}\n{item.get('url', '')}".strip()


async def _reference(ex) -> dict[str, Any] | None:
    """BTC, ETH and the alt basket's last prices now: the labels' anchor. None if the tickers can't be read (the
    event is still logged; the labeller marks it no_data)."""
    try:
        perps, tickers = await asyncio.gather(ex.coin_perps(), ex.public.fetch_tickers())
    except Exception:  # noqa: BLE001 - research anchor only; the alert still goes out
        log.exception("market news: tickers unavailable, event logged without reference prices")
        return None
    px = {s: (tickers.get(s) or {}).get("last") for s in MAJORS}
    alts = sorted((s for s in perps if s not in MAJORS and (tickers.get(s) or {}).get("last")),
                  key=lambda s: -((tickers[s].get("quoteVolume")) or 0))[:ALT_N]
    return {"btc": px[MAJORS[0]], "eth": px[MAJORS[1]], "alts": {s: tickers[s]["last"] for s in alts}}


def _context(journal) -> dict[str, Any] | None:
    """What the market map showed when the item was seen: did the move come before the headline?"""
    from .market.market_map import cached
    m = cached(journal, 1800)
    if not m:
        return None
    h = m["header"]
    return {"map_ts": m["ts"], "regime": h.get("regime"), "breadth_pct": h.get("breadth_pct"), "btc": h.get("btc"),
            "eth": h.get("eth"), "btc_15m": m.get("btc_15m"), "eth_15m": m.get("eth_15m")}


def _sent_last_hour(journal, now: float) -> int:
    since = (datetime.fromtimestamp(now, UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    return int(journal.db.execute("SELECT COUNT(*) FROM alerts WHERE kind = ? AND ts >= ?", (KIND, since)).fetchone()[0])

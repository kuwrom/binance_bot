"""Free news sources: Binance announcements (public CMS JSON), crypto RSS feeds, Fear & Greed.

Titles, summaries and links are third-party text: cleaned on the way in (untrusted.clean), and returned to the agent
only wrapped as {"untrusted_text", "source"} (`for_agent`, `coin_news`, `announcements`; signals wrap the text they
put in features). The raw, cleaned items (`news_for`, `listing_announcements`) are for matching and dedupe."""

from __future__ import annotations

import asyncio
import calendar
import re
import time
from datetime import UTC, datetime
from typing import Any

import feedparser
import httpx

from .untrusted import clean, safe_url, wrap

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/128 Safari/537.36"}
CMS_LIST = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
ANNOUNCE_URL = "https://www.binance.com/en/support/announcement/detail/{code}"
CATALOGS = {48: "New Listing", 49: "Binance News", 161: "Delisting", 93: "Activities"}
RSS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "The Block": "https://www.theblock.co/rss.xml",
    "Decrypt": "https://decrypt.co/feed",
    "BeInCrypto": "https://beincrypto.com/feed/",
    "NewsBTC": "https://www.newsbtc.com/feed/",
    "CryptoPotato": "https://cryptopotato.com/feed/",
    "U.Today": "https://u.today/rss",
}
BEARISH_PATTERNS = (r"delist\w*", r"monitoring tag", r"hack(ed|er|s)?", r"exploit\w*", r"unlock\w*", r"lawsuit",
                    r"sec", r"investigation", r"outflows?", r"dump(s|ed|ing)?", r"rug ?pull", r"suspend\w*",
                    r"halt\w*", r"insolven\w*", r"breach\w*", r"drain(ed)?")
_BEARISH_RE = re.compile(r"\b(" + "|".join(BEARISH_PATTERNS) + r")\b", re.IGNORECASE)
# Tickers that are also ordinary words/abbreviations: only match as $TICK, (TICK) or "TICK token/price/coin".
COMMON_WORD_TICKERS = {"PEOPLE", "TRUMP", "ONE", "US", "AI", "BANK", "GAS", "HIGH", "LOW", "MOVE", "ME", "IT", "ON",
                       "SUN", "NOT", "BIG", "GOOD", "HOT", "KEY", "CAT", "DOG", "MAX", "OM", "ID", "AT", "OP", "ACE",
                       "ALT", "BOND", "CHESS", "COOKIE", "DOGS", "FUN", "GALA", "MAGIC", "MASK", "PORTAL", "SAFE",
                       "STORJ", "TRUST", "USUAL", "VANA", "WAL", "WIN", "ZK", "Q", "S", "T", "W", "U", "B", "A"}
# Project names -> ticker, so "Solana" headlines match SOL. Extend as the desk trades new coins.
ALIASES = {"BTC": ["Bitcoin"], "ETH": ["Ethereum", "Ether"], "SOL": ["Solana"], "XRP": ["Ripple"],
           "DOGE": ["Dogecoin"], "ADA": ["Cardano"], "AVAX": ["Avalanche"], "LINK": ["Chainlink"],
           "DOT": ["Polkadot"], "QNT": ["Quant"], "WLD": ["Worldcoin", "World Network"], "PYTH": ["Pyth"],
           "SUI": ["Sui Network"], "TON": ["Toncoin"], "NEAR": ["NEAR Protocol"], "APT": ["Aptos"],
           "ARB": ["Arbitrum"], "OP": ["Optimism"], "INJ": ["Injective"], "TIA": ["Celestia"], "SEI": ["Sei Network"]}
_MULTIPLIER = re.compile(r"^(1000000|100000|10000|1000|1M)(?=[A-Z])")
_cache: dict[str, tuple[float, Any]] = {}
_locks: dict[tuple[str, int], asyncio.Lock] = {}
LISTING_TTL, LISTING_PAGES = 6 * 3600, 2


async def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    # one fetch per key at a time: a scan's ~25 coin contexts arriving together share it (no stampede);
    # locks are per event loop so separate loops (tests, CLI runs) never share one
    lock = _locks.setdefault((key, id(asyncio.get_running_loop())), asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = await fn()
        _cache[key] = (time.time(), val)
        return val


def normalize_base(base: str) -> str:
    """'1000PEPE' -> 'PEPE', '1MBABYDOGE' -> 'BABYDOGE'."""
    return _MULTIPLIER.sub("", base.upper())


def mentions(text: str, base: str) -> bool:
    """Does a headline mention this coin? Case-sensitive ticker match with context rules for tickers that
    are ordinary words or 1-2 letters, plus project-name aliases."""
    base = normalize_base(base)
    tick = re.escape(base)
    if base in COMMON_WORD_TICKERS or len(base) <= 2:
        pat = rf"(\${tick}\b|\({tick}\)|\b{tick}\s+(token|coin|price|perp|futures)\b)"
        if re.search(pat, text, re.IGNORECASE if len(base) > 2 else 0):
            return True
    elif re.search(rf"(?<![A-Za-z0-9$]){tick}(?![A-Za-z0-9])|\${tick}\b", text):
        return True
    return any(re.search(rf"\b{re.escape(n)}\b", text, re.IGNORECASE) for n in ALIASES.get(base, []))


def bearish_keywords(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in _BEARISH_RE.finditer(text)})


async def binance_announcements(per_catalog: int = 15) -> list[dict]:
    async def fetch():
        out: list[dict] = []
        async with httpx.AsyncClient(headers=UA, timeout=15) as c:
            for cid, name in CATALOGS.items():
                r = await c.get(CMS_LIST, params={"type": 1, "catalogId": cid, "pageNo": 1, "pageSize": per_catalog})
                r.raise_for_status()
                for cat in r.json().get("data", {}).get("catalogs", []):
                    for a in cat.get("articles", []):
                        out.append({"catalog": name, "title": clean(a["title"]),
                                    "time": _iso(a["releaseDate"] / 1000),
                                    "url": safe_url(ANNOUNCE_URL.format(code=a["code"]))})
        return sorted(out, key=lambda x: x["time"], reverse=True)
    return await _cached("ann", 120, fetch)


async def listing_announcements(pages: int = LISTING_PAGES) -> list[dict]:
    """The new-listing catalog, deeper than binance_announcements (pages x 50 items, about 1-2 months), cached 6h:
    the source of a listing (HODLer airdrop, Launchpool, Seed tag, futures-only...) for signals/supply.py."""
    async def fetch():
        out: list[dict] = []
        async with httpx.AsyncClient(headers=UA, timeout=15) as c:
            for page in range(1, pages + 1):
                r = await c.get(CMS_LIST, params={"type": 1, "catalogId": 48, "pageNo": page, "pageSize": 50})
                r.raise_for_status()
                for cat in r.json().get("data", {}).get("catalogs", []):
                    for a in cat.get("articles", []):
                        out.append({"title": clean(a["title"]), "time": _iso(a["releaseDate"] / 1000),
                                    "url": safe_url(ANNOUNCE_URL.format(code=a["code"]))})
        return sorted(out, key=lambda x: x["time"], reverse=True)
    return await _cached("ann_listing", LISTING_TTL, fetch)


async def rss_headlines(max_age_h: float = 48) -> list[dict]:
    """Every feed's recent items. A feed that fails is skipped; when every feed fails this raises (news_for reports
    it as an error: no headlines from dead feeds is not "no news")."""
    async def fetch_one(c: httpx.AsyncClient, src: str, url: str) -> list[dict] | None:
        try:
            r = await c.get(url)
            r.raise_for_status()
            feed = await asyncio.to_thread(feedparser.parse, r.content)   # CPU-bound: off the event loop
        except Exception:
            return None
        items = []
        for e in feed.entries[:40]:
            ts = e.get("published_parsed") or e.get("updated_parsed")
            if not ts:
                continue  # undated items would look fresh forever
            epoch = calendar.timegm(ts)  # feedparser structs are UTC
            if time.time() - epoch <= max_age_h * 3600:
                items.append({"source": src, "title": clean(e.get("title", "")), "time": _iso(epoch),
                              "url": safe_url(e.get("link", "")), "summary": clean(_strip(e.get("summary", "")))})
        return items

    async def fetch():
        async with httpx.AsyncClient(headers=UA, timeout=15, follow_redirects=True) as c:
            batches = await asyncio.gather(*(fetch_one(c, s, u) for s, u in RSS_FEEDS.items()))
        if all(b is None for b in batches):
            raise RuntimeError(f"every RSS feed failed ({len(batches)})")   # raised, so never cached
        return sorted((i for b in batches if b for i in b), key=lambda x: x["time"], reverse=True)
    return await _cached("rss", 300, fetch)


async def fear_greed() -> dict:
    async def fetch():
        async with httpx.AsyncClient(timeout=10) as c:
            d = (await c.get("https://api.alternative.me/fng/", params={"limit": 2})).json()["data"]
        return {"value": int(d[0]["value"]), "label": d[0]["value_classification"],
                "yesterday": int(d[1]["value"]) if len(d) > 1 else None}
    return await _cached("fng", 900, fetch)


async def news_for(base: str, max_items: int = 15) -> dict:
    """Announcements + headlines mentioning a coin ticker, with bearish keywords flagged. Raw: internal only (the
    guardian matches and dedupes on it); the agent gets `coin_news`."""
    answers = await asyncio.gather(binance_announcements(), rss_headlines(), return_exceptions=True)
    got_ann, got_rss = answers   # a separate unpack: unpacked in place, mypy can't type these (has-type)
    errors = [n for n, v in (("binance_announcements", got_ann), ("rss", got_rss)) if isinstance(v, BaseException)]
    ann = got_ann if isinstance(got_ann, list) else []
    rss = got_rss if isinstance(got_rss, list) else []
    hits_a = [a for a in ann if mentions(a["title"], base)]
    hits_r = [h for h in rss if mentions(h["title"] + " " + h["summary"], base)]
    for h in hits_a + hits_r:
        h["bearish_keywords"] = bearish_keywords(h["title"])
    return {"coin": normalize_base(base), "binance_announcements": hits_a[:max_items],
            "headlines": hits_r[:max_items], "errors": errors,
            "note": "news sources failed: absence of news is NOT confirmed" if errors else None}


def for_agent(res: dict) -> dict:
    """A `news_for` result as the agent sees it: every title and summary wrapped as untrusted data."""
    return {**res, "binance_announcements": [_for_agent(a) for a in res.get("binance_announcements") or []],
            "headlines": [_for_agent(h) for h in res.get("headlines") or []]}


async def coin_news(base: str, max_items: int = 15) -> dict:
    return for_agent(await news_for(base, max_items))


async def announcements(limit: int = 20) -> list[dict]:
    """Latest Binance announcements as the agent sees them (titles wrapped as untrusted data)."""
    return [_for_agent(a) for a in (await binance_announcements())[:limit]]


def source_of(item: dict) -> str:
    """Where a news item came from, as untrusted wrappers name it."""
    return f"rss:{item['source']}" if item.get("source") else "binance_announcements"


def _for_agent(item: dict) -> dict:
    """Our own fields as they are; the third-party text only inside {"untrusted_text", "source"}."""
    src = source_of(item)
    out = {k: item[k] for k in ("catalog", "time", "bearish_keywords") if k in item}
    out.update(url=safe_url(item.get("url")), title=wrap(item.get("title"), src))
    if item.get("summary"):
        out["summary"] = wrap(item["summary"], src)
    return out


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="minutes")


def _strip(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()

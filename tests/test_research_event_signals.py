import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from shortbot import news
from shortbot.market.hub import MarketContext
from shortbot.signals import REGISTRY
from shortbot.signals.hack import hack_flag, match
from shortbot.signals.supply import classify, listing_supply, supply_facts

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="minutes")


def test_both_signals_are_registered_in_the_event_group_and_on_by_default():
    for name in ("listing_supply", "hack_flag"):
        assert REGISTRY[name].group == "event" and REGISTRY[name].default_on


def test_listing_source_takes_the_most_specific_announcement():
    t = ["Binance Futures Will Launch USDⓈ-Margined OPGUSDT Perpetual Contract (2026-09-01)",
         "Binance Will List OpenGradient (OPG) with Seed Tag Applied",
         "Introducing OpenGradient (OPG) on Binance HODLer Airdrops! Earn OPG With Retroactive BNB"]
    assert classify(t) == "hodler_airdrop"
    assert classify(t[:2]) == "seed_spot" and classify(t[:1]) == "futures_only"
    assert classify(["Binance Alpha Will Remove MTP, BDXN (2026-06-30)"]) is None
    assert classify(["Binance Will Add X (X) on Earn, Buy Crypto, Convert"]) is None


def test_supply_facts():
    f = supply_facts({"circulating_supply": 150, "total_supply": 1000, "market_cap": 3e8,
                      "fully_diluted_valuation": 2e9})
    assert f == {"float_ratio": 0.15, "fdv_usd": 2e9, "mcap_usd": 3e8, "fdv_mcap": 6.67}
    assert supply_facts({"circulating_supply": 50, "total_supply": None, "max_supply": 100})["float_ratio"] == 0.5
    assert supply_facts(None)["float_ratio"] is None


class Ctx:
    def __init__(self, age, anns=(), sup=None, items=()):
        self.age, self.anns, self.sup, self.items, self.calls = age, list(anns), sup, list(items), []

    async def supply(self): return self.sup
    async def market_info(self): return {"listing_age_days": self.age}

    async def listing_news(self):
        self.calls.append("listing_news")
        return self.anns

    async def news(self): return {"binance_announcements": [], "headlines": self.items}


async def test_listing_supply_tags_new_listings_only_and_never_scores():
    anns = [{"title": "Introducing Kite (KITE) on Binance Launchpool! Farm KITE", "time": iso(400)}]
    out = await listing_supply(Ctx(20, anns, {"circulating_supply": 18, "total_supply": 100}))
    assert out.features["listing_source"] == "launchpool" and out.features["float_ratio"] == 0.18
    assert out.points == [] and out.vetoes == []
    old = Ctx(200, anns)
    out = await listing_supply(old)
    assert out.features["listing_source"] is None and old.calls == []      # no announcement lookup past 90 days


def test_hack_match_window_newest_and_reimbursement():
    items = [{"title": "XYZ protocol exploited, $40M drained", "time": iso(5), "source": "The Block", "url": "u1"},
             {"title": "XYZ hacked again", "time": iso(90), "source": "CoinDesk", "url": "u2"},          # too old
             {"title": "XYZ team announces bounty for the attacker", "time": iso(2), "source": "Decrypt", "url": "u3"},
             {"title": "Hackathon winners announced on XYZ", "time": iso(1), "source": "X", "url": "u4"}]
    m = match(items, NOW)
    assert m["hack_flag"] and m["hack_url"] == "u3" and m["hack_hits"] == 2 and m["hack_reimbursement"]
    assert m["hack_age_h"] == 2.0 and m["hack_source"] == "Decrypt"
    assert match(items[1:2] + items[3:], NOW) == {"hack_flag": False}


async def test_hack_signal_is_a_feature_only():
    out = await hack_flag(Ctx(20, items=[{"title": "ABC exploit drains vault", "time": iso(1)}]))
    assert out.features["hack_flag"] is True and out.features["hack_source"] == "Binance"
    assert out.points == [] and out.vetoes == []


async def test_listing_news_matches_futures_contract_names(monkeypatch):
    async def fake_listings():
        return [{"title": "Binance Futures Will Launch USDⓈ-Margined MARSCOINUSDT Perpetual Contract",
                 "time": iso(9)},
                {"title": "Binance Will List MarsCoin (MARSCOIN) with Seed Tag Applied", "time": iso(10)},
                {"title": "Binance Will List Other (OTH)", "time": iso(11)}]
    monkeypatch.setattr(news, "listing_announcements", fake_listings)

    ctx = MarketContext(SimpleNamespace(ex=None, sem=asyncio.Semaphore(2)), "MARSCOIN/USDT:USDT", None)
    got = await ctx.listing_news()
    assert len(got) == 2 and all("MARSCOIN" in a["title"] for a in got) and "listing_news" not in ctx.missing


async def test_cached_fetch_is_shared_by_concurrent_callers():
    calls = []

    async def fetch():
        calls.append(1)
        await asyncio.sleep(0.01)
        return ["x"]
    news._cache.pop("t_stampede", None)
    res = await asyncio.gather(*(news._cached("t_stampede", 60, fetch) for _ in range(25)))
    assert len(calls) == 1 and all(r == ["x"] for r in res)

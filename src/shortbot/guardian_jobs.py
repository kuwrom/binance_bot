"""Guardian market-wide and research jobs (monitor and research lanes). None of them changes orders: they alert,
record metrics and label research data, so a slow or hung one never touches position protection."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from . import news, research
from .guardian_loop import guarded
from .market.regime import market_regime
from .market.social import snapshot as social_snapshot
from .probability.labels import label_due as label_predictions
from .risk_budget import governor_status

log = logging.getLogger("shortbot.guardian")
SOCIAL_TOP_MOVERS = 30


class MarketJobs:
    """Guardian mixin: needs app, ex, j, n, entries, liq_store, social_store, _social_task, _refresh_proc."""

    # ------------------------------------------------------------ monitor lane
    async def check_announcements(self) -> None:
        items = await news.binance_announcements()
        first_run = self.j.get("ann_initialised") is None
        for a in items:
            if a["catalog"] not in ("New Listing", "Delisting"):
                continue
            key = f"ann_seen:{a['url']}"
            if self.j.get(key) is None:
                self.j.set(key, str(time.time()))
                fresh = datetime.fromisoformat(a["time"]) > datetime.now(timezone.utc) - timedelta(hours=24)
                if not first_run and fresh:  # never alert on old items that merely re-appear in the list
                    await self.n.send(f"binance_{a['catalog'].lower().replace(' ', '_')}", f"{a['title']}\n{a['url']}",
                                      cooldown_s=0)
        self.j.set("ann_initialised", "1")

    async def check_governor(self) -> None:
        """Account breakers: tell the human the moment new entries are halted (open trades keep their stops)."""
        if not self.ex.trade:
            return
        reg = (await market_regime(self.ex))["verdict_for_shorts"]
        g = await governor_status(self.app.trader, reg)
        if g.get("halt"):
            await self.n.send("governor_halt", f"New entries halted until {g.get('until') or 'human review'}: "
                              + "; ".join(g["reasons"]), urgent=True, cooldown_s=4 * 3600)

    async def record_social(self) -> None:
        """Hourly attention snapshots for movers + held/pending coins. Square is throttled (~3 s/request), so this
        runs as its own task: it must never hold up the monitor lane either."""
        if self._social_task and not self._social_task.done():
            return

        async def run() -> None:
            perps = await self.ex.coin_perps()
            tickers = await self.ex.public.fetch_tickers()
            movers = sorted((t for sym, t in tickers.items() if sym in perps),
                            key=lambda t: -abs(t.get("percentage") or 0))[:SOCIAL_TOP_MOVERS]
            syms = {t["symbol"] for t in movers} | {t["symbol"] for t in self.j.open_trades()} | \
                {e["symbol"] for e in self.entries.pending()}
            bases = sorted({x.split("/")[0].removeprefix("1000") for x in syms})
            n = await social_snapshot(self.social_store, bases)
            log.info("social snapshot: %d rows for %d coins", n, len(bases))

        self._social_task = asyncio.create_task(guarded("social", run(), self.n))

    async def research_refresh(self) -> None:
        """Weekly walk-forward refresh (refresh.py) in a child process: the guardian never waits on it."""
        from . import refresh
        if (self._refresh_proc and self._refresh_proc.returncode is None) or not refresh.due(self.j):
            return
        self.j.set(refresh.KV_KEY, str(time.time()))   # claim the week now, so a restart doesn't spawn twice
        self._refresh_proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "shortbot.refresh", stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        await self.n.send("research_refresh", "weekly research refresh started (data, backtest ledger, registry)",
                          None, cooldown_s=3600)

    async def housekeeping(self) -> None:
        n = self.j.prune_kv(("news_seen:", "ann_seen:", "alert_ts:"), 30 * 86400)
        m = self.liq_store.prune()
        log.info("pruned %d kv markers, %d old liquidations", n, m)

    # ------------------------------------------------------------ research lane
    async def record_metrics(self) -> None:
        perps = await self.ex.coin_perps()
        tickers, rates = await asyncio.gather(self.ex.public.fetch_tickers(), self.ex.public.fetch_funding_rates())
        ts = datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()
        movers = sorted((t for s, t in tickers.items() if s in perps),
                        key=lambda t: t.get("percentage") or 0, reverse=True)[:20]
        held = {t["symbol"] for t in self.j.open_trades()}
        oi: dict[str, float] = {}
        for sym in {t["symbol"] for t in movers} | held:
            try:
                oi[sym] = (await self.ex.public.fetch_open_interest(sym)).get("openInterestAmount")
            except Exception:  # noqa: BLE001
                pass
        self.j.record_metrics([(ts, s, t.get("last"), rates.get(s, {}).get("fundingRate"), oi.get(s),
                                t.get("quoteVolume")) for s, t in tickers.items() if s in perps])

    async def label_research(self) -> None:
        now = int(time.time() * 1000)
        n = await research.label_outcomes(self.ex, self.j, now)
        m = await label_predictions(self.ex, self.j, now)
        x = await research.label_post_exit(self.ex, self.j, now)
        from .laggard import label as label_laggards
        x += await label_laggards(self.ex, self.j)
        from . import registry
        from .registry_seed import feed_candidates
        fed = feed_candidates(self.j) if n else 0          # newly labelled scans -> registry node episodes
        for a in registry.decay_alarms(self.j.db):
            await self.n.send("pattern_decay", f"{a['id']} ({a['status']}): {a['verdict']}", None, cooldown_s=86_400)
        if n or m or x:
            log.info("labelled %d research candidates, %d predictions, %d post-exit moves; %d registry episodes",
                     n, m, x, fed)

    async def refresh_calendar(self) -> None:
        """Macro calendar (BLS, ForexFactory weekly, FOMC). Twice a day: ForexFactory rate-limits hard."""
        from .market.calendar import MacroCalendar
        log.info("macro calendar: %s", await MacroCalendar(self.app.settings.db_path).refresh())

    async def market_map(self) -> None:
        """Whole-tape map every 10 min (docs/MARKET_MAP.md): cached for the sector signal, logged for research."""
        from .market.market_map import build_map, log_snapshot
        m = await build_map(self.ex, self.j)
        log_snapshot(self.j, m)
        if m["vetoes"]["pause"]:
            await self.n.send("market_pause", "; ".join(m["vetoes"]["pause"]), cooldown_s=3600)

    async def sweep_study(self) -> None:
        """Daily: extend the level-sweep study with the last ~day of 1m data (regime-aware stop guide)."""
        from .sweeps import run_study
        r = await run_study(self.ex, self.j, days=1.1, top=60)
        log.info("sweep study: %s events from %s coins", r["events_logged"], r["coins"])

    async def refresh_supply(self) -> None:
        """Weekly CoinGecko supply facts (float, FDV) for the controller label; the daily tick is a no-op."""
        from .market.supply import refresh
        log.info("supply: %s", await refresh(self.j))

    async def ladder_shadow(self) -> None:
        """Shadow mode (docs/LADDER.md): log the 3-level ladder and the exposure the rule WOULD hold. No orders."""
        from .ladder import build_ladder
        for p in await self.app.trader.positions():
            try:
                r = await build_ladder(self.ex, self.j, p)
                if r["action"] not in ("hold", "wait"):
                    await self.n.send("ladder_shadow", f"shadow rule would {r['action']} to {r['x_virtual']:.0%} "
                                      f"(score {r['score']:+}, {r['parts']})", p["symbol"], cooldown_s=1800)
            except Exception:  # noqa: BLE001 - research logging never blocks the guardian
                log.exception("ladder %s failed", p["symbol"])

    async def laggard_study(self) -> None:
        """Shadow study (docs/LADDER.md A): log sector-leader breakdowns with their laggards and a random control."""
        from .laggard import detect
        n = await detect(self.ex, self.j)
        if n:
            log.info("laggard study: %d new events", n)

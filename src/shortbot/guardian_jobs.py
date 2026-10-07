"""Guardian market-wide and research jobs (monitor and research lanes). None of them changes orders: they alert,
record metrics and label research data, so a slow or hung one never touches position protection."""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from . import entry_gates, gateway_guardian, gateway_health, news, procs, research, scan_job, setup_ladder, stream_task
from . import killswitch as ks
from .exchange import to_raw
from .guardian_loop import guarded, send_safe
from .journal import Journal
from .market.regime import market_regime
from .market.social import snapshot as social_snapshot
from .probability.labels import label_due as label_predictions
from .risk_budget import governor_status

if TYPE_CHECKING:
    from pathlib import Path

    from .app import App
    from .entries import EntryDesk
    from .exchange import Exchanges
    from .gateway import Gateway
    from .gateway_guardian import Link
    from .market.liquidations import LiquidationStore
    from .market.social import SocialStore
    from .notify import Notifier
    from .scan_tiered import TieredScan

log = logging.getLogger("shortbot.guardian")
SOCIAL_TOP_MOVERS = 30
LADDER_CLEAR_REMIND_S = 7 * 86400    # live: criteria pass, a human clear is due: alerted once, then weekly
LADDER_STOP_LOOKBACK_S = 7 * 86400   # an older setup_ladder_stop row is history (the brief shows it), not news
KV_STOP_SENT = "setup_ladder_stop_sent:"   # + alert id: that row went to Telegram


class MarketJobs:
    """Guardian mixin; the Guardian attributes it uses are declared below."""
    app: App
    ex: Exchanges
    j: Journal
    n: Notifier
    entries: EntryDesk
    liq_store: LiquidationStore
    social_store: SocialStore
    streams_db: Path | None
    tiered: TieredScan
    _social_task: asyncio.Task | None
    _refresh_proc: asyncio.subprocess.Process | None
    gw: Gateway
    gateway_link: Link

    # ------------------------------------------------------------ monitor lane
    async def check_announcements(self) -> None:
        items = await news.binance_announcements()
        first_run = self.j.get(self.j.mode_key("ann_initialised")) is None
        for a in items:
            if a["catalog"] not in ("New Listing", "Delisting"):
                continue
            key = self.j.mode_key(f"ann_seen:{a['url']}")
            if self.j.get(key) is None:
                self.j.mark(key, str(time.time()))
                fresh = datetime.fromisoformat(a["time"]) > datetime.now(UTC) - timedelta(hours=24)
                if not first_run and fresh:  # never alert on old items that merely re-appear in the list
                    await self._announce(a)
        self.j.mark(self.j.mode_key("ann_initialised"), "1")

    async def _announce(self, a: dict) -> None:
        """A delisting naming a coin on our book (an open short, or an entry that is or may be resting) is urgent, per
        symbol: the position must be out before settlement (delisting-short skill). Anything else is a heads-up."""
        kind, text = f"binance_{a['catalog'].lower().replace(' ', '_')}", f"{a['title']}\n{a['url']}"
        book = {t["symbol"] for t in self.j.open_trades()} | \
            {e["symbol"] for e in self.entries.pending(all_orders=True)}
        hits = sorted(s for s in book if _names(a["title"], s)) if a["catalog"] == "Delisting" else []
        for sym in hits:
            await self.n.send(kind, f"Delisting of a coin on our book: exit before settlement (delisting-short "
                              f"playbook).\n{text}", sym, urgent=True, cooldown_s=0)
        if not hits and self.app.settings.guardian_market:   # the heads-up once, from the market guardian
            await self.n.send(kind, text, cooldown_s=0)

    async def check_governor(self) -> None:
        """Account breakers: tell the human the moment new entries are halted (open trades keep their stops)."""
        if not self.ex.trade:
            return
        reg = (await market_regime(self.ex))["verdict_for_shorts"]
        lim, src = ks.effective_limits(self.app.settings, self.j)   # what the session sizes and places under (#12)
        g = await governor_status(self.app.trader, reg, limits=lim)
        entry_gates.publish_governor(self.j, g, src)   # the protective lane withdraws resting entries on a halt
        await guarded("kill_switch", ks.trip_on_drawdown(self.j, self.app.settings, g.get("drawdown_pct")), self.n)
        if g.get("halt"):
            await self.n.send("governor_halt", f"New entries halted until {g.get('until') or 'human review'} "
                              f"({src}), resting ones withdrawn: " + "; ".join(g["reasons"]), urgent=True,
                              cooldown_s=4 * 3600)

    async def record_social(self) -> None:
        """Hourly attention snapshots for movers + held/pending coins. Square is throttled (~3 s/request), so this
        runs as its own task: it must never hold up the monitor lane either."""
        if self._social_task and not self._social_task.done():
            return

        async def run() -> None:
            bases = sorted({x.split("/")[0].removeprefix("1000") for x in await self._attention_symbols()})
            n = await social_snapshot(self.social_store, bases)
            log.info("social snapshot: %d rows for %d coins", n, len(bases))

        self._social_task = asyncio.create_task(guarded("social", run(), self.n))

    async def _attention_symbols(self) -> set[str]:
        """The top movers by |24h| plus every coin held or resting, on both desks."""
        perps = await self.ex.coin_perps()
        tickers = await self.ex.public.fetch_tickers()
        movers = sorted((t for sym, t in tickers.items() if sym in perps),
                        key=lambda t: -abs(t.get("percentage") or 0))[:SOCIAL_TOP_MOVERS]
        return {t["symbol"] for t in movers} | {t["symbol"] for t in self.j.open_trades(all_modes=True)} | \
            {e["symbol"] for e in self.entries.pending()}

    async def refresh_onchain(self) -> None:
        """On-chain shadow features (market/onchain.py) for the same coins as the social snapshot, every 10 min:
        scans read the kv snapshot and never call the web3 endpoints themselves."""
        from .market import onchain
        log.info("onchain: %s", await onchain.refresh(self.j, sorted(await self._attention_symbols())))

    async def research_refresh(self) -> None:
        """Weekly walk-forward refresh (refresh.py) in a child process: the guardian never waits on it."""
        from . import refresh
        if (self._refresh_proc and self._refresh_proc.returncode is None) or not refresh.due(self.j):
            return
        await self.j.write(lambda j: j.set(refresh.KV_KEY, str(time.time())))   # claimed first: never spawned twice
        self._refresh_proc = await refresh.spawn()     # its own process group: stop_refresh ends it with its steps
        await self.n.send("research_refresh", "weekly research refresh started (data, backtest ledger, registry)",
                          None, cooldown_s=3600)

    async def stop_refresh(self) -> None:
        """Shutdown (#80): the refresh child and every step it started end with the guardian. A run cut short is
        retried in RETRY_S, as a failed run is (the week was claimed when it started)."""
        from . import refresh
        proc, self._refresh_proc = self._refresh_proc, None
        try:
            if await refresh.stop(proc):
                retry = str(time.time() - refresh.EVERY_S + refresh.RETRY_S)
                await self.j.write(lambda j: j.set(refresh.KV_KEY, retry))
        except Exception as e:  # noqa: BLE001 - shutdown goes on; the human is told
            await send_safe(self.n, "research_refresh_stop_failed", f"the research refresh (pid {proc and proc.pid}) "
                            f"may still be running: {type(e).__name__}: {e}", cooldown_s=0)

    async def check_processes(self) -> None:
        """Hourly (#58): live shortbot-mcp / shortbot-guardian processes that are unregistered or run code older than
        the code on disk ignore fixes such as the kill switch and the symbol lock. Alert each; never signal one."""
        findings, dead, _, _ = await procs.check(self.j)
        procs.forget(self.j, dead)
        for f in findings:
            await self.n.send(f"stale_process:{f['pid']}", f["text"], urgent=True, cooldown_s=procs.ALERT_COOLDOWN_S)

    async def check_market_feed(self) -> None:
        """A guardian without the market jobs: is the market guardian's feed fresh (market_feed.py)?"""
        from . import market_feed
        why = market_feed.problem(self.j)
        if why:
            await self.n.send("market_feed_stale", why, urgent=True, cooldown_s=market_feed.STALE_S * 2)

    async def check_gateway(self) -> None:
        """Every 30 s (#36 step 4): with SHORTBOT_GATEWAY on, a stale or missing order-gateway heartbeat is alerted:
        the gateway can't report its own death (gateway_health.py). Step 6: can this guardian reach it (kv
        gateway_client:guardian, for the deploy health check)? A gateway that beats but stays unreachable from here
        for gateway_health.DOWN_URGENT_S is alerted: every guardian write runs as break-glass meanwhile."""
        rec = await gateway_guardian.ping(self.gw, self.j)
        await gateway_health.watch(self.j, self.app.settings, self.n)
        down = self.gateway_link.outage_s()
        if rec["client"] and not rec["ok"] and down >= gateway_health.DOWN_URGENT_S and \
                gateway_health.age_s(self.j) <= gateway_health.STALE_S:
            await self.n.send("gateway_unreachable", f"The order gateway beats but the guardian hasn't reached it for "
                              f"{down:.0f} s ({rec['why']}): every guardian write runs as break-glass. Check "
                              "`docker compose logs gateway` and data/run (deploy/DEPLOY.md §4a).", cooldown_s=1800,
                              urgent=True)

    async def housekeeping(self) -> None:
        n = await self.j.write(Journal.prune_kv, ("news_seen:", "ann_seen:", "alert_ts:", KV_STOP_SENT), 30 * 86400)
        n += await self.j.write(Journal.prune_kv, (scan_job.KV_SEEN,), scan_job.RENOTIFY_S)   # alert dedup, expired
        m = await self.liq_store.prune()
        b = await asyncio.to_thread(stream_task.prune, self.streams_db) if self.streams_db else 0
        log.info("pruned %d kv markers, %d old liquidations, %d old stream bars", n, m, b)

    async def setup_ladder_check(self) -> None:
        """Daily (#69): the frozen re-enable check on every SHADOW setup ladder of this account. Demo re-enables at
        HALF (a decision and an alert); live alerts that the criteria pass and a human clear is due; failing criteria
        are only logged. A ladder that could not be evaluated fails the job (guardian_error) after the others ran."""
        live = self.app.settings.mode == "live"
        broken = []
        for row in await self.j.write(setup_ladder.auto_reenable, live=live):
            s = row["setup"]
            if row.get("error"):
                broken.append(f"{s}: {row['error']}")
            elif row.get("ok"):
                await self._ladder_reenabled(row)
            elif live and row.get("fails") == []:      # criteria pass; only the human clear is missing
                await self.n.send(f"setup_ladder_clear_due:{s}", f"criteria pass for {s} ({_evidence(row)}): awaiting "
                                  f"`shortbot-admin ladder-clear --setup {s} --reason …`",
                                  cooldown_s=LADDER_CLEAR_REMIND_S)
            else:
                log.info("setup ladder %s stays SHADOW: %s", s, "; ".join(row.get("fails") or [str(row.get("reason"))]))
        if broken:
            raise RuntimeError("setup ladder not evaluated for " + "; ".join(broken))

    async def _ladder_reenabled(self, row: dict) -> None:
        s = row["setup"]
        try:
            need = (setup_ladder.status(self.j, s).get("probation") or {}).get("need_r")
        except Exception:  # noqa: BLE001 - the re-enable is done: journal and alert it anyway
            log.exception("setup ladder %s: probation unreadable after its re-enable", s)
            need = None
        ev = {k: row.get(k) for k in ("n", "days", "mean", "t_day", "reenabled_at")}
        self.j.log_decision("setup_ladder_reenable", f"{s} re-enabled at HALF: the criteria frozen at its stop pass "
                            f"on the shadow evidence since ({_evidence(row)})",
                            snapshot={"setup": s, "evidence": ev, "probation_need_r": need})
        await self.n.send("setup_ladder_reenable", f"setup {s} re-enabled at HALF (probation until +{need}R): "
                          f"criteria {_evidence(row)}", cooldown_s=0)

    async def forward_ladder_stops(self) -> None:
        """Every 10 min (#69): setup_ladder.status() journals a `setup_ladder_stop` row in whichever process saw the
        stop first (often the MCP server sizing a trade), and nothing sends it. Send each of this account's recent
        rows to Telegram once (no second row); a failed send releases its marker for the next pass."""
        since = (datetime.now(UTC) - timedelta(seconds=LADDER_STOP_LOOKBACK_S)).isoformat(timespec="seconds")
        rows = self.j.db.execute("SELECT id, symbol, message FROM alerts WHERE kind = 'setup_ladder_stop' AND mode = ? "
                                 "AND ts >= ? ORDER BY id", (self.j.mode, since)).fetchall()
        for aid, sym, msg in rows:
            marker = KV_STOP_SENT + str(aid)
            claimed = await self.j.write(lambda j, k=marker: j.db.execute(
                "INSERT OR IGNORE INTO kv (k, v) VALUES (?, ?)", (k, str(time.time()))).rowcount)
            if claimed and not await self.n.forward("setup_ladder_stop", msg, sym):
                await self.j.write(lambda j, k=marker: j.db.execute("DELETE FROM kv WHERE k = ?", (k,)))

    # ------------------------------------------------------------ research lane
    async def record_metrics(self) -> None:
        perps = await self.ex.coin_perps()
        tickers, rates = await asyncio.gather(self.ex.public.fetch_tickers(), self.ex.public.fetch_funding_rates())
        ts = datetime.now(UTC).replace(second=0, microsecond=0).isoformat()
        movers = sorted((t for s, t in tickers.items() if s in perps),
                        key=lambda t: t.get("percentage") or 0, reverse=True)[:20]
        held = {t["symbol"] for t in self.j.open_trades(all_modes=True)}   # market data: both desks' books
        oi: dict[str, float] = {}
        for sym in {t["symbol"] for t in movers} | held:
            try:
                oi[sym] = (await self.ex.public.fetch_open_interest(sym)).get("openInterestAmount")
            except Exception:  # noqa: BLE001 - research metrics: a coin without OI is recorded without it
                pass
        await self.j.write(Journal.record_metrics, [(ts, s, t.get("last"), rates.get(s, {}).get("fundingRate"),
                                                     oi.get(s), t.get("quoteVolume")) for s, t in tickers.items()
                                                    if s in perps])

    async def label_research(self) -> None:
        now = int(time.time() * 1000)
        n = await research.label_outcomes(self.ex, self.j, now)
        m = await label_predictions(self.ex, self.j, now)
        x = await research.label_post_exit(self.ex, self.j, now)
        from .laggard import label as label_laggards
        x += await label_laggards(self.ex, self.j)
        from . import registry
        path = self.j.w.path   # labelled scans -> registry node episodes, off the loop (in-memory journal: none)
        fed = await asyncio.to_thread(_feed, path, self.j.mode) if n and path else 0
        for a in registry.decay_alarms(self.j.db):
            await self.n.send(f"pattern_decay:{a['id']}", f"{a['id']} ({a['status']}): {a['verdict']}", None,
                              cooldown_s=86_400)
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
        await self.j.write(log_snapshot, m)
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

    async def long_shadow(self) -> None:
        """Phase 0 of the long build (docs/LONG_BUILD.md): the playbook's forward test as shadow trades. No orders.
        Polled every 15 min; works once per daily close (long_shadow.due)."""
        from . import long_shadow
        if long_shadow.due(int(time.time() * 1000)):
            log.info("long shadow: %s", await long_shadow.run(self.ex))

    async def tiered_scan(self) -> None:
        """Tiers 1-3 of the scanner (scan_tiered.py, docs/SCANNING.md): triggers at each 15m close and on fast events,
        deep scores of the selected coins under the tier-2 cap, research candidates, alerts."""
        await self.tiered.step()

    async def cadence_shadow(self) -> None:
        """Forward check of a per-rule tier-1 cadence (cadence_shadow.py, docs/SCANNING.md "Cadence"): logs the minutes
        two rules hold on live 1m bars. No deep scores, no alerts, no orders."""
        from .market import stream_read
        store = stream_read.path_for(self.app.settings.db_path)
        if store is not None and store.exists():
            r = await asyncio.to_thread(_off_loop, "cadence_shadow", store, int(time.time() * 1000))
            log.debug("cadence shadow: %s", r)

    async def mtf_views(self) -> None:
        """Every streamed coin's 5m/15m/1h/4h/1d state, logged once per 15m close (mtf_live.py). Local reads only."""
        from .market import stream_read
        store = stream_read.path_for(self.app.settings.db_path)
        if store is not None and store.exists():
            r = await asyncio.to_thread(_off_loop, "mtf_live", store, int(time.time() * 1000))
            if r.get("logged"):
                log.info("mtf views: %d coins at %s", r["logged"], r["close"])

    async def laggard_study(self) -> None:
        """Shadow study (docs/LADDER.md A): log sector-leader breakdowns with their laggards and a random control."""
        from .laggard import detect
        n = await detect(self.ex, self.j)
        if n:
            log.info("laggard study: %d new events", n)


def _feed(path: str, mode: str) -> int:
    """registry_seed.feed_candidates in a worker thread, on its own connection: it rebuilds every node's forward rows
    from all labelled candidates (25-93 ms of CPU on the 2026-10 journal, growing with it) and may wait for the write
    lock; neither is the loop's (#79)."""
    from .registry_seed import feed_candidates
    j = Journal(path, mode)
    try:
        return feed_candidates(j)
    finally:
        j.db.close()


def _off_loop(module: str, *args: Any) -> Any:
    """A scan-lane shadow job, imported AND run in the worker thread: the first import of mtf_live pulls in pandas,
    ~0.2 s that would stall every lane on the loop (#78)."""
    return importlib.import_module(f"{__package__}.{module}").run(*args)


def _evidence(row: dict) -> str:
    return f"{row.get('n')} episodes over {row.get('days')} days, mean {row.get('mean')}, t_day {row.get('t_day')}"


def _names(title: str, symbol: str) -> bool:
    """Does an announcement title name this perp? Its coin (news.mentions) or its contract ('ALPACAUSDT')."""
    return news.mentions(title, symbol.split("/")[0]) or bool(
        re.search(rf"(?<![A-Z0-9]){re.escape(to_raw(symbol))}(?![A-Z0-9])", title))

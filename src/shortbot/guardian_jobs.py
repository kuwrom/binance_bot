"""Guardian market-wide and research jobs (monitor and research lanes). None of them changes orders: they alert,
record metrics and label research data, so a slow or hung one never touches position protection."""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import killswitch as ks
from . import entry_gates, news, procs, research, scan_job, setup_ladder, stream_task
from .exchange import to_raw
from .guardian_loop import guarded
from .market.regime import market_regime
from .market.social import snapshot as social_snapshot
from .probability.labels import label_due as label_predictions
from .risk_budget import governor_status

log = logging.getLogger("shortbot.guardian")
SOCIAL_TOP_MOVERS = 30
LADDER_CLEAR_REMIND_S = 7 * 86400    # live: criteria pass, a human clear is due: alerted once, then weekly
LADDER_STOP_LOOKBACK_S = 7 * 86400   # an older setup_ladder_stop row is history (the brief shows it), not news
KV_STOP_SENT = "setup_ladder_stop_sent:"   # + alert id: that row went to Telegram


class MarketJobs:
    """Guardian mixin: needs app, ex, j, n, entries, liq_store, social_store, _social_task, _refresh_proc, streams_db,
    tiered."""

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
                    await self._announce(a)
        self.j.set("ann_initialised", "1")

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
        if not hits:
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

    async def check_processes(self) -> None:
        """Hourly (#58): live shortbot-mcp / shortbot-guardian processes that are unregistered or run code older than
        the code on disk ignore fixes such as the kill switch and the symbol lock. Alert each; never signal one."""
        findings, dead, _ = await procs.check(self.j)
        procs.forget(self.j, dead)
        for f in findings:
            await self.n.send(f"stale_process:{f['pid']}", f["text"], urgent=True, cooldown_s=procs.ALERT_COOLDOWN_S)

    async def housekeeping(self) -> None:
        n = self.j.prune_kv(("news_seen:", "ann_seen:", "alert_ts:", KV_STOP_SENT), 30 * 86400)
        n += self.j.prune_kv((scan_job.KV_SEEN,), scan_job.RENOTIFY_S)   # scan alert dedup: past its window
        m = self.liq_store.prune()
        b = await asyncio.to_thread(stream_task.prune, self.streams_db) if self.streams_db else 0
        log.info("pruned %d kv markers, %d old liquidations, %d old stream bars", n, m, b)

    async def setup_ladder_check(self) -> None:
        """Daily (#69): the frozen re-enable check on every SHADOW setup ladder of this account. Demo re-enables at
        HALF (a decision and an alert); live alerts that the criteria pass and a human clear is due; failing criteria
        are only logged. A ladder that could not be evaluated fails the job (guardian_error) after the others ran."""
        live = self.app.settings.mode == "live"
        broken = []
        for row in setup_ladder.auto_reenable(self.j, live=live):
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
        since = (datetime.now(timezone.utc) - timedelta(seconds=LADDER_STOP_LOOKBACK_S)).isoformat(timespec="seconds")
        rows = self.j.db.execute("SELECT id, symbol, message FROM alerts WHERE kind = 'setup_ladder_stop' AND mode = ? "
                                 "AND ts >= ? ORDER BY id", (self.j.mode, since)).fetchall()
        for aid, sym, msg in rows:
            marker = KV_STOP_SENT + str(aid)
            claimed = self.j.db.execute("INSERT OR IGNORE INTO kv (k, v) VALUES (?, ?)",
                                        (marker, str(time.time()))).rowcount
            self.j.db.commit()
            if claimed and not await self.n.forward("setup_ladder_stop", msg, sym):
                self.j.db.execute("DELETE FROM kv WHERE k = ?", (marker,))
                self.j.db.commit()

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
            except Exception:  # noqa: BLE001 - research metrics: a coin without OI is recorded without it
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

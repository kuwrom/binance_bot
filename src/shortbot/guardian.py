"""Always-on guardian (runs on AWS). It never OPENS trades. Each cycle it:
- reconciles the journal with the exchange (stop-outs, liquidations, manual closes -> real PnL)
- restores missing stops (force-closes after repeated failures)
- executes the pre-approved parts of each trade's plan (breakeven, trailing, targets/time stop if auto)
- tracks MAE/MFE per trade, alerts on squeeze risk, funding flips, stop proximity, news
- alerts on new listing/delisting announcements; records metrics and labels research candidates
Every position and every job is isolated (and time-boxed): one failure or hang never stops the others.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from . import indicators as ind
from . import news, reconcile, research
from .app import close_app, get_app
from .entries import EntryDesk
from .probability.labels import label_due as label_predictions
from .market.liquidations import LiquidationRecorder, LiquidationStore
from .market.social import SocialStore
from .market.social import snapshot as social_snapshot
from .exchange import to_raw
from .manager import excursions, plan_actions
from .market.regime import market_regime
from .notify import Notifier
from .risk_budget import governor_status, track_day_peak
from .stops import is_clock_drift

log = logging.getLogger("shortbot.guardian")
JOBS_EVERY_S = {"entries": 5, "positions": 60, "governor": 300, "announcements": 180, "metrics": 1800,
                "research": 3600, "social": 3600, "calendar": 43200, "market_map": 600, "sweeps": 86400, "supply": 86400, "ladder": 900, "laggard": 900,
                "housekeeping": 86400, "refresh": 3600}
LONG_JOBS = {"sweeps": 1800, "supply": 900, "market_map": 900, "laggard": 600, "research": 600}  # else 240 s
SOCIAL_TOP_MOVERS = 30
SQUEEZE_PRICE_1H_PCT = 5.0
SQUEEZE_OI_1H_PCT = 5.0
STOP_NEAR_PCT = 1.0
RESYNC_EVERY_S = 600
FALLBACK_STOP_PCT = 10.0
MAX_STOP_RESTORE_FAILS = 3


class Guardian:
    """Dumb and fixed on purpose: uses only the .env baseline limits, never AI research overrides."""

    def __init__(self, app=None):
        self.app = app or get_app(research=False)
        self.n = Notifier(self.app.settings, self.app.journal)
        self.app.trader._alert = self.n.as_trader_alert
        self.ex = self.app.ex
        self.j = self.app.journal
        self.stop_fails: dict[str, int] = {}
        self._last_resync = 0.0
        self.entries = EntryDesk(self.app.trader)
        self.liq_store = LiquidationStore(self.app.settings.db_path)
        self.social_store = SocialStore(self.app.settings.db_path)
        self._social_task: asyncio.Task | None = None
        self._refresh_proc: asyncio.subprocess.Process | None = None
        self._fund_ts: dict[str, float] = {}

    # ------------------------------------------------------------ positions
    async def check_positions(self) -> None:
        if not self.ex.trade:
            return
        tr = self.app.trader
        if time.time() - self._last_resync > RESYNC_EVERY_S:   # the Mac's clock drifts after sleep (-1021)
            await self.ex.resync_time()
            self._last_resync = time.time()
        live = await tr._raw_positions()
        for res in await reconcile.reconcile(tr, live):
            await self.n.send("trade_closed", f"{res['reason']}: net {res['net']:+.2f} USDT (fees {res['fees']:.2f}, "
                              f"funding {res['funding']:.2f})", res["symbol"], cooldown_s=0, urgent=res["liquidated"])
        acct = await tr.account(live)
        track_day_peak(self.j, acct["day_pnl"])   # arms the give-back stop (governor.py)
        lim = self.app.settings.limits
        if acct["equity"] and acct["day_pnl"] / acct["equity"] * 100 <= -lim.daily_loss_limit_pct:
            await self.n.send("daily_loss_limit", f"Day PnL {acct['day_pnl']} USDT. New shorts are blocked until "
                              "00:00 UTC. Review open positions.", urgent=True, cooldown_s=6 * 3600)
        for p in await tr.positions():
            try:
                await self._one_position(p)
            except Exception as e:  # noqa: BLE001 - isolate: other positions still get checked
                if is_clock_drift(e):
                    await self.ex.resync_time()   # the loop-level resync never sees errors swallowed here
                log.exception("position %s failed", p["symbol"])
                await self.n.send("position_check_error", f"{type(e).__name__}: {e}", p["symbol"], cooldown_s=1800)

    async def _one_position(self, p: dict) -> None:
        sym = p["symbol"]
        if p["side"] != "short":
            await self.n.send("unexpected_long", f"A LONG position exists ({p['contracts']}). This desk is "
                              "short-only; close it manually.", sym, urgent=True)
            return
        if p["UNPROTECTED"]:
            await self._restore_stop(sym)
            return
        if p["journal_status"] == "NOT IN JOURNAL":
            await self.n.send("untracked_position", "Position not opened via shortbot: stop is enforced but no "
                              "plan/thesis. Journal it or close it.", sym, cooldown_s=86400)
        if p["stop_distance_pct"] is not None and p["stop_distance_pct"] < STOP_NEAR_PCT:
            await self.n.send("stop_near", f"Mark {p['mark']} is {p['stop_distance_pct']}% from stop "
                              f"{p['exchange_stop']}. PnL {p['unrealized_pnl']} USDT.", sym, cooldown_s=1800)
        trade = self.j.active_trade(sym)
        if trade and trade["status"] == "open" and trade.get("entry_price"):
            await self._run_plan(trade, p)
        await self._squeeze_and_funding(sym)
        await self._news(sym)

    async def _restore_stop(self, sym: str) -> None:
        tr = self.app.trader
        try:
            res = await tr.ensure_stop(sym, FALLBACK_STOP_PCT)
            self.stop_fails.pop(sym, None)
            await self.n.send("stop_missing", f"No exchange stop found. Action: {res}", sym, urgent=True, cooldown_s=300)
        except Exception as e:  # noqa: BLE001
            n = self.stop_fails[sym] = self.stop_fails.get(sym, 0) + 1
            if n >= MAX_STOP_RESTORE_FAILS:
                pos = await tr.position(sym)
                if pos:
                    await tr._market_close(sym, float(pos["contracts"]))
                self.stop_fails.pop(sym, None)
                await self.n.send("forced_close", f"Could not restore a stop {n}x ({e}); position CLOSED.", sym,
                                  urgent=True, cooldown_s=0)
            else:
                await self.n.send("stop_restore_failed", f"attempt {n}/{MAX_STOP_RESTORE_FAILS}: {e}", sym,
                                  urgent=True, cooldown_s=0)

    async def _run_plan(self, trade: dict, p: dict) -> None:
        ex = excursions(trade, p["mark"])
        self.j.update_trade(trade["id"], mfe_pct=round(ex["mfe_pct"], 3), mae_pct=round(ex["mae_pct"], 3),
                            low_since_entry=ex["low_since_entry"])
        trade = {**trade, **ex}
        tr, sym = self.app.trader, trade["symbol"]
        hits = list(trade.get("targets_hit") or [])
        for a in plan_actions(trade, p):
            if a.kind == "alert":
                await self.n.send(f"plan:{a.reason.split(':')[0][:30]}", a.reason, sym, cooldown_s=a.cooldown_s,
                                  urgent=a.urgent)
            elif a.kind == "move_stop":
                r = await tr.move_stop(sym, a.price, f"plan/{a.reason}")
                if r.get("noop"):          # rounds to the stop already in place
                    continue
                await self.n.send("plan_stop_moved" if r.get("ok") else "plan_stop_move_failed",
                                  f"{a.reason} -> {a.price:.6g}: {r}", sym, cooldown_s=0)
            elif a.kind == "target_done":
                hits.append(a.target_price)                 # one running list: two actions in one cycle both stick
                self.j.update_trade(trade["id"], targets_hit=list(hits))
                await self.n.send("plan_target_done", a.reason, sym, cooldown_s=0)
            elif a.kind == "close":
                r = await tr.close(sym, f"plan/{a.reason}", fraction=a.fraction or 1.0)
                if a.target_price is not None and r.get("ok"):
                    hits.append(a.target_price)
                    self.j.update_trade(trade["id"], targets_hit=list(hits))
                await self.n.send("plan_close", f"{a.reason}: {r}", sym, cooldown_s=0)
                if (a.fraction or 1.0) >= 1.0:
                    return

    async def _track_funding(self, sym: str) -> None:
        """Funding paid so far on the open trade (every 15 min), so the plan's funding budget can act on it."""
        trade = self.j.active_trade(sym)
        if not trade or not trade.get("opened_at") or not self.ex.trade:
            return
        if time.time() - self._fund_ts.get(sym, 0) < 900:
            return
        self._fund_ts[sym] = time.time()
        since = int(datetime.fromisoformat(trade["opened_at"]).timestamp() * 1000)
        rows = await self.ex.trade.fapiPrivateGetIncome({"symbol": to_raw(sym), "incomeType": "FUNDING_FEE",
                                                         "startTime": since, "limit": 1000})
        self.j.update_trade(trade["id"], funding=round(sum(float(r.get("income") or 0) for r in rows), 6))

    async def _squeeze_and_funding(self, sym: str) -> None:
        try:
            await self._track_funding(sym)
        except Exception as e:  # noqa: BLE001 - tracking must never block the squeeze checks
            log.warning("funding tracking %s: %s", sym, e)
        pub = self.ex.public
        c5, oi, fr, intervals = await asyncio.gather(
            pub.fetch_ohlcv(sym, "5m", limit=13), self.ex.oi_history(sym, "5m", 13),
            pub.fetch_funding_rate(sym), self.ex.funding_intervals(), return_exceptions=True)
        if isinstance(c5, list) and len(c5) >= 13:
            px = ind.pct_change(c5[0][4], c5[-1][4])
            oi_chg = ind.pct_change(oi[0]["oi"], oi[-1]["oi"]) if isinstance(oi, list) and len(oi) > 1 else 0
            if px >= SQUEEZE_PRICE_1H_PCT and oi_chg >= SQUEEZE_OI_1H_PCT:
                await self.n.send("squeeze_risk", f"Price +{px:.1f}% and OI +{oi_chg:.1f}% in 1h. Re-evaluate now.",
                                  sym, urgent=True, cooldown_s=1800)
            elif px >= SQUEEZE_PRICE_1H_PCT * 1.5:
                await self.n.send("price_spike", f"Price +{px:.1f}% in 1h against the short.", sym, urgent=True,
                                  cooldown_s=1800)
        if isinstance(fr, dict) and (fr.get("fundingRate") or 0) < 0:
            await self.n.send("funding_negative", f"Funding {fr['fundingRate'] * 100:.4f}%: shorts now PAY and the "
                              "trade is crowded. Playbook: consider cutting.", sym, cooldown_s=4 * 3600)
        if isinstance(intervals, dict) and intervals.get(to_raw(sym), {}).get("interval_h") == 1:
            await self.n.send("funding_hourly", "Funding switched to 1h settlement (cap hit): extreme regime.", sym,
                              urgent=True, cooldown_s=4 * 3600)

    async def _news(self, sym: str) -> None:
        res = await news.news_for(sym.split("/")[0], max_items=5)
        for item in res["binance_announcements"] + res["headlines"]:
            url = item.get("url", "")
            if url and self.j.get(f"news_seen:{url}") is None:
                self.j.set(f"news_seen:{url}", str(time.time()))
                await self.n.send("news_on_position", f"{item['title']}\n{url}", sym, cooldown_s=0)

    # ------------------------------------------------------------ limit entries (5 s)
    async def check_entries(self) -> None:
        if not self.ex.trade or not self.entries.pending():
            return
        for ev in await self.entries.poll():
            if ev["event"] in ("cancelled", "expired"):
                await self.n.send(f"entry_{ev['event']}", f"entry #{ev['entry_id']}: {ev.get('why', '')}",
                                  cooldown_s=0)

    # ------------------------------------------------------------ market-wide
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

    async def refresh_supply(self) -> None:
        """Weekly CoinGecko supply facts (float, FDV) for the controller label; the daily tick is a no-op."""
        from .market.supply import refresh
        log.info("supply: %s", await refresh(self.j))

    async def refresh_calendar(self) -> None:
        """Macro calendar (BLS, ForexFactory weekly, FOMC). Twice a day: ForexFactory rate-limits hard."""
        from .market.calendar import MacroCalendar
        log.info("macro calendar: %s", await MacroCalendar(self.app.settings.db_path).refresh())

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

    async def record_social(self) -> None:
        """Hourly attention snapshots for movers + held/pending coins. Square is throttled (~3 s/request), so this
        runs as a BACKGROUND task: it must never delay the 5 s fill checks or the 60 s position checks."""
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

        self._social_task = asyncio.create_task(run())

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

    # ------------------------------------------------------------ loop
    async def run(self) -> None:
        await self.n.send("guardian_started", f"Guardian online ({self.app.settings.mode}). Trading keys: "
                          f"{'yes' if self.ex.trade else 'NO (monitor-only)'}", cooldown_s=0)
        jobs = {"entries": self.check_entries, "positions": self.check_positions, "governor": self.check_governor,
                "announcements": self.check_announcements,
                "metrics": self.record_metrics, "research": self.label_research, "social": self.record_social,
                "calendar": self.refresh_calendar, "market_map": self.market_map,
                "sweeps": self.sweep_study, "supply": self.refresh_supply,
                "ladder": self.ladder_shadow, "laggard": self.laggard_study,
                "housekeeping": self.housekeeping, "refresh": self.research_refresh}
        last = dict.fromkeys(jobs, 0.0)
        # continuous liquidation recording (websocket; the only source of Binance liquidation data)
        self._liq_task = asyncio.create_task(LiquidationRecorder(self.liq_store).run())
        while True:
            for name, fn in jobs.items():
                if time.time() - last[name] >= JOBS_EVERY_S[name]:
                    last[name] = time.time()
                    try:   # a hung await (socket, DB lock) must never freeze the protective loop
                        await asyncio.wait_for(fn(), LONG_JOBS.get(name, 240))
                    except Exception as e:  # noqa: BLE001  (incl. TimeoutError from wait_for)
                        if "-1021" in str(e) or type(e).__name__ == "InvalidNonce":
                            await self.ex.resync_time()  # clock drift: resync and retry next cycle
                            last[name] = 0.0
                            log.warning("clock drift (-1021) in %s: resynced time", name)
                            continue
                        log.exception("%s failed", name)
                        await self.n.send(f"guardian_error:{name}", f"{type(e).__name__}: {e}", cooldown_s=1800)
            self.j.set("guardian_heartbeat", str(time.time()))   # the review loop checks its age
            await asyncio.sleep(5)


async def _main() -> None:
    try:
        await Guardian().run()
    finally:
        await close_app()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(_main())


if __name__ == "__main__":
    main()

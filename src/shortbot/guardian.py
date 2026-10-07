"""Always-on guardian (runs on AWS). It never OPENS trades. It:
- reconciles the journal with the exchange (stop-outs, liquidations, manual closes -> real PnL)
- restores missing stops (force-closes after repeated failures)
- executes the pre-approved parts of each trade's plan (breakeven, trailing, targets/time stop if auto)
- tracks MAE/MFE per trade, alerts on squeeze risk, funding flips, stop proximity, news
- alerts on new listing/delisting announcements; records metrics and labels research candidates
- streams the liquid universe (stream_task.py) and runs the tiered scan on it (scan_tiered.py, docs/SCANNING.md)
- listens to the account's user-data stream (user_stream.py, #54): an event about one of our orders runs the fill poll
  or the positions pass at once (TRIGGER_GAP_S apart at most); the polls stay the truth and keep their schedule

Jobs run in lanes (guardian_loop.py) that run concurrently: protective (fills, stops, plans), watch (alerts on
open shorts), monitor (account and market alerts), research, scan (tiers 1-3 at each 15m close) and coins (the focus
coins' memory, guardian_coins.py). Every position and every job is isolated and
time-boxed, and a slow or hung job only ever delays its own lane, never the protective one. No journal write waits on
the event loop for another process's write lock (#79, journal_writer.py): a job awaits a write it needs (only its lane
waits) or posts bookkeeping (nothing waits); a wait past STALL_ALERT_S is alerted with the lock's holder.
"""

from __future__ import annotations

import asyncio
import fcntl
import functools
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import audit, procs, symlock, uptime, weight_ledger
from .app import FLUSH_S, close_app, get_app
from .config import Settings
from .entries import EntryDesk
from .gateway_guardian import Link, for_guardian
from .gateway_wire import secure_dir
from .guardian_coins import CoinJobs
from .guardian_jobs import MarketJobs
from .guardian_loop import ESCALATE_AFTER_FAILS, Job, Lane, Streak, send_safe, supervised
from .guardian_margin import MarginChecks
from .guardian_plan import PlanChecks
from .guardian_positions import PositionChecks
from .guardian_watch import WatchChecks
from .killswitch import heartbeat_key
from .margin import ADL_PERSIST_PASSES
from .market import stream_read
from .market.liquidations import LiquidationRecorder, LiquidationStore
from .market.social import SocialStore
from .notify import Notifier
from .redact import install_log_redaction
from .scan_tiered import TieredScan
from .stream_task import StreamTask
from .symlock import GUARDIAN_WAIT_S, ReconcileBusy
from .user_stream import UserStream

log = logging.getLogger("shortbot.guardian")
# job: (every_s, timeout_s). Jobs of one lane run one after another; the lanes run side by side.
# Protective streaks escalate. The heartbeat ("protection last verified", #61) is stamped by the positions pass alone
# (PositionChecks.check_positions), and only while the fill poll's last run succeeded. entry_reconcile is bookkeeping
# (the fill poll resolves unknown sends itself; the positions pass protects any position they made): no heartbeat.
PROTECTIVE = {"entries": (5, 240), "positions": (60, 240), "entry_reconcile": (60, 240)}
WATCH = {"watch": (60, 240)}   # streaks escalate too (silent squeeze checks matter on a short book); no heartbeat
# what is and isn't happening while a job keeps failing (the urgent escalation, guardian_loop.Lane)
UNVERIFIED = "the heartbeat stops, so new entries are refused (and HALTED after 10 min) until it works again."
FAILING = {
    "positions": "Stops, plans and closed trades are NOT being checked: check positions and stops on the exchange. "
                 "Resting stops still fire on Binance; " + UNVERIFIED,
    "entries": "The fill poll is NOT running: a resting entry that fills keeps only its pre-armed stop (a send of "
               "unknown outcome has none) until the positions pass restores one, and isn't journalled; the "
               "withdrawal sweeps aren't running either. Check resting entries on Binance; " + UNVERIFIED,
    "entry_reconcile": "Bookkeeping only: sends still 'placing' aren't timed out, unresolved pre-armed stops aren't "
                       "matched, open orders aren't swept for external ones and intents of unknown outcome aren't "
                       "resolved (re-read the position after an unknown close or add). Fills, stops and plans ARE "
                       "still checked (the fill poll resolves unknown sends itself): the heartbeat and entries are "
                       "unaffected.",
    "watch": "Squeeze, funding, news and ADL alerts on open shorts are NOT running: watch the positions yourself.",
}
MONITOR = {"governor": (300, 240), "announcements": (180, 240), "social": (3600, 240), "refresh": (3600, 240),
           "housekeeping": (86400, 240), "processes": (3600, 240), "margin_mode": (300, 240), "gateway": (30, 30),
           "setup_ladder": (86400, 240), "setup_ladder_stops": (600, 120),
           "market_feed": (300, 30)}   # only without the market jobs: is the market guardian's feed fresh?   # #69: re-enable check; stops to Telegram
RESEARCH = {"metrics": (1800, 240), "research": (3600, 600), "calendar": (43200, 240), "market_map": (600, 900),
            "sweeps": (86400, 1800), "supply": (86400, 900), "ladder": (900, 240), "laggard": (900, 600),
            "long_shadow": (900, 900), "onchain": (600, 300)}
SCAN = {"scan": (15, 300),    # tiers 1-3 (#72): each 15m close, fast events between closes (they decay in 10-45 min)
        "cadence_shadow": (60, 50),   # #40: logs two rules every minute on live bars; no deep scores, no alerts
        "mtf_views": (60, 50)}        # #41: every coin's 5m-1d state at each 15m close, logged; no exchange calls
COINS = {"coin_memory": (900, 300)}   # focus coins: state, events, break scores (coin_memory.py); alerts, never acts
# Market-wide jobs: one guardian runs them for both desks (Settings.guardian_market; the live guardian sets it off,
# docs/DESKS.md). The rest, and coin_memory on its own book, are this account's and always run.
MARKET_JOBS = frozenset({"social", "refresh", "housekeeping", "processes", "metrics", "research", "calendar",
                         "market_map", "sweeps", "supply", "laggard", "long_shadow", "scan", "cadence_shadow",
                         "mtf_views", "onchain"})
RESEARCH_START_DELAY_S = 30   # the first protective pass after a (re)start runs on its own
DELAYED_LANES = ("research", "scan", "coins")   # they start RESEARCH_START_DELAY_S after the others
# a user-data event runs its job at once, but no sooner than this after the job's last start: a burst is one run, and
# the positions pass (account and income reads) runs at most 4x a minute however many stop or target events come
TRIGGER_GAP_S = {"entries": 1.0, "positions": 15.0}
TICK_S = 1.0
STALL_ALERT_S = 60.0   # a journal write waiting this long for another process's lock is alerted, naming the holder


class Guardian(PositionChecks, PlanChecks, WatchChecks, MarginChecks, MarketJobs, CoinJobs):
    """Dumb and fixed on purpose: uses only the desk baseline (.env + desk limits), never AI research overrides. One exception:
    the kill-switch loss trips (daily loss, dd_stop) and the governor verdict (its halt withdraws resting entries,
    entry_gates.py) follow an active experiment's limits, the ones the session places and sizes under."""

    def __init__(self, app=None):
        self.app = app or get_app(research=False)
        weight_ledger.fail_fast()   # this event loop never waits on the host-wide weight ledger (busy_timeout 0)
        self.n = Notifier(self.app.settings, self.app.journal)
        self.app.trader.set_alert(self.n.as_trader_alert)
        self.app.trader.symlocks.wait_s = GUARDIAN_WAIT_S   # a busy symbol is skipped, never waited out
        self.ex = self.app.ex
        self.j = self.app.journal
        self.stop_fails: dict[str, int] = {}
        self.pos_streak = Streak(ESCALATE_AFTER_FAILS)   # consecutive failed checks per symbol
        self.watch_streak = Streak(ESCALATE_AFTER_FAILS)   # consecutive failed watch passes per symbol (#57)
        self.pnl_streak = Streak(ESCALATE_AFTER_FAILS)     # consecutive passes without a day P&L (income unreadable)
        self.adl_streak = Streak(ADL_PERSIST_PASSES)       # consecutive watch passes in the top ADL quantile (#64)
        self._adl_logged: dict[str, tuple[int, float]] = {}
        self.pnl_tripped = False                           # this outage already tripped REDUCING (once per outage)
        self.untracked_since: dict[str, float] = {}       # symbol -> first seen without a journal trade
        self.kill_seen: dict | None = None                # last kill-switch state seen (loosening tripwire)
        self._last_resync = 0.0
        self._last_beat: float | None = None               # this process's last verified heartbeat (uptime.py)
        self.entries = EntryDesk(self.app.trader)
        # its protective actions are intents (#36 step 2), sent to the order gateway since step 6 (gateway_guardian.py)
        self.gateway_link = Link()
        self.gw = for_guardian(lambda: self.j, lambda: self.app.settings, self, self.n, self.gateway_link)
        self.entries.gw = for_guardian(lambda: self.j, lambda: self.app.settings, self.entries, self.n,
                                       self.gateway_link)
        self.liq_store = LiquidationStore(self.app.settings.db_path)
        self.social_store = SocialStore(self.app.settings.db_path)
        self.j.w.on_stall = self._journal_stalled
        self._stall_alerts: set[asyncio.Task] = set()
        self._social_task: asyncio.Task | None = None
        self._refresh_proc: asyncio.subprocess.Process | None = None
        self._fund_ts: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []
        self.lanes: dict[str, Lane] = {}
        self.streams_db = stream_read.path_for(self.app.settings.db_path)   # None for an in-memory journal (tests)
        self.tiered = TieredScan(self.ex, self.j, self.n, self.streams_db, self.app.settings.db_path)
        self.user_stream = (UserStream(self.app.trader, self.n, self._on_stream_event, extra_stats=self._trigger_stats)
                            if self.ex.trade else None)   # monitor-only: no account, no stream

    def _lanes(self) -> dict[str, Lane]:
        fns = {"entries": self.check_entries, "positions": self._leased("positions pass", self.check_positions),
               "entry_reconcile": self._leased("entry_reconcile", self.reconcile_entries), "watch": self.watch_positions,
               "governor": self.check_governor, "announcements": self.check_announcements,
               "social": self.record_social, "refresh": self.research_refresh, "housekeeping": self.housekeeping,
               "processes": self.check_processes, "margin_mode": self.check_margin_mode,
               "gateway": self.check_gateway,
               "metrics": self.record_metrics, "research": self.label_research, "calendar": self.refresh_calendar,
               "market_map": self.market_map, "sweeps": self.sweep_study, "supply": self.refresh_supply,
               "onchain": self.refresh_onchain,
               "ladder": self.ladder_shadow, "laggard": self.laggard_study, "long_shadow": self.long_shadow,
               "scan": self.tiered_scan, "cadence_shadow": self.cadence_shadow, "mtf_views": self.mtf_views,
               "setup_ladder": self.setup_ladder_check, "setup_ladder_stops": self.forward_ladder_stops,
               "coin_memory": self.coin_memory, "market_feed": self.check_market_feed}

        market = self.app.settings.guardian_market

        def lane(name: str, table: dict, **kw) -> Lane:
            jobs = [Job(k, fns[k], *v) for k, v in table.items()
                    if (k not in MARKET_JOBS or market) and (k != "market_feed" or not market)]
            return Lane(name, jobs, self.n, self.ex, self.j, TICK_S, **kw)
        lanes = {"protective": lane("protective", PROTECTIVE, escalate_after=ESCALATE_AFTER_FAILS,
                                    escalate_note=FAILING),
                 "watch": lane("watch", WATCH, escalate_after=ESCALATE_AFTER_FAILS, escalate_note=FAILING),
                 "monitor": lane("monitor", MONITOR), "research": lane("research", RESEARCH),
                 "scan": lane("scan", SCAN), "coins": lane("coins", COINS)}
        return {name: ln for name, ln in lanes.items() if ln.jobs}

    def _leased(self, what: str, fn: Callable[[], Awaitable[Any]]) -> Callable[[], Awaitable[None]]:
        """A reconciling job under the reconcile lease (symlock.reconciling, GATEWAY §5): while the gateway's start-up
        reconciles (every deploy restarts both), the pass is skipped after GUARDIAN_WAIT_S: no failure, no heartbeat;
        the next one runs on what the gateway left. Never two reconcilers at once."""
        async def run() -> None:
            try:
                async with self.app.trader.symlocks.reconciling(f"guardian {what}", symlock.GUARDIAN_WAIT_S):
                    await fn()
            except ReconcileBusy as e:
                log.info("%s skipped: %s", what, e)
        return run

    def _on_stream_event(self, hit, at: float) -> None:
        """A user-data event about one of our orders (user_events.py): its job runs now, `at` = when it arrived."""
        lane = self.lanes.get("protective")
        if lane:
            lane.trigger(hit.job, why=f"{hit.type} {hit.kind} {hit.status} {hit.symbol}", at=at,
                         min_gap_s=TRIGGER_GAP_S.get(hit.job, 0.0))

    def _trigger_stats(self) -> dict:
        lane = self.lanes.get("protective")
        return dict(lane.trigger_stats) if lane else {}

    def _triggered(self, job: str) -> bool:
        """Is `job` running ahead of its schedule, woken by a user-data event?"""
        lane = self.lanes.get("protective")
        return bool(lane and lane.triggered(job))

    def _heartbeat(self, at: float) -> None:
        """Called by a positions pass that verified every held position (or a flat account), `at` = its position read.
        Stamped only while the fill poll's last run succeeded: fills are protected (and journalled) by it. Its age is
        how long protection has gone unverified; every reader acts on that age (killswitch.watch_guardian). Posted
        (#79): another process's write lock delays it, never the pass. A gap since the stamp before (this process's,
        else the previous process's) is logged (uptime.py, #46)."""
        lane = self.lanes.get("protective")
        if lane and lane.failing("entries"):
            log.info("heartbeat withheld: the fill poll is failing")
            return
        first = self._last_beat is None
        prev = uptime.stamp_of(self.j.get(heartbeat_key(self.j))) if first else self._last_beat
        self._last_beat = at
        self.j.post(lambda j: j.set(heartbeat_key(j), str(at)))
        uptime.beat(self.j, prev, at, first)

    def _journal_stalled(self, waited: float, who: dict | None) -> None:
        """journal_writer: its writes have waited `waited` s for another process's write lock (#79). From STALL_ALERT_S
        on, the human is told who holds it: the loop runs on, but bookkeeping and the heartbeat queue behind it."""
        if waited < STALL_ALERT_S:
            return
        holder = f"pid {who['pid']} ({who['cmd']})" if who else "an unknown process"
        t = asyncio.get_running_loop().create_task(send_safe(
            self.n, "journal_locked", f"journal writes have waited {waited:.0f} s for the write lock, held by "
            f"{holder}. Stops are still restored (their intents land late); the heartbeat and bookkeeping are queued "
            "until it frees.", urgent=True, cooldown_s=3600))
        self._stall_alerts.add(t)
        t.add_done_callback(self._stall_alerts.discard)

    # ------------------------------------------------------------ run
    async def _startup(self) -> None:
        await send_safe(self.n, "guardian_started", f"Guardian online ({self.app.settings.mode}). Trading keys: "
                        f"{'yes' if self.ex.trade else 'NO (monitor-only)'}", cooldown_s=0)
        for why in getattr(self.app.settings, "invalid", ()):   # a bad value fell back to its default (config.py)
            await send_safe(self.n, "config_invalid", f"{why}. Fix it in .env and restart the desk (deploy/DEPLOY.md "
                            "§4a).", urgent=True, cooldown_s=0)
        procs.register(self.j, "guardian", self.app.settings.mode)   # #58: the processes job compares its code
        db = self.app.settings.db_path   # resolve() and docker_vm's file reads: off the loop
        vm = await asyncio.to_thread(lambda: procs.docker_vm(Path(db).expanduser().resolve().parent))
        if vm:
            await send_safe(self.n, "guardian_docker_vm", f"The guardian runs in a container behind a VM: {vm}. flock "
                            "on data/locks and SQLite WAL don't cross the VM boundary, so it is NOT serialised with "
                            "the MCP servers and CLI on the host. Run it natively (deploy/DEPLOY.md section 4).",
                            urgent=True, cooldown_s=0)

    async def run(self) -> None:
        await self._startup()
        self.lanes = self._lanes()
        runs: dict[str, Callable[[], Awaitable[Any]]] = {
            name: functools.partial(ln.run, RESEARCH_START_DELAY_S if name in DELAYED_LANES else 0.0)
            for name, ln in self.lanes.items()}
        if self.app.settings.guardian_market:   # market data: recorded once, by the market guardian (MARKET_JOBS)
            # continuous liquidation recording (websocket; the only source of Binance liquidation data)
            runs["liquidations"] = LiquidationRecorder(self.liq_store).run
            if self.streams_db:   # tier 0: market streams into the local store, on its own thread (stream_task.py)
                runs["streams"] = StreamTask(self.ex, self.streams_db).run
        if self.user_stream:   # fills and stop-outs pushed by Binance: latency only, the polls stay the truth
            runs["user_stream"] = self.user_stream.run
        self._tasks = [asyncio.create_task(supervised(name, fn, self.n), name=f"guardian:{name}")
                       for name, fn in runs.items()]
        try:
            await asyncio.gather(*self._tasks)   # supervised tasks never return: this waits for cancellation
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        tasks = [t for t in (*self._tasks, self._social_task) if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.stop_refresh()   # after the lanes: none can start another one now
        for store in (self.liq_store, self.social_store):   # the journal itself drains in close_app (#79)
            await store.w.drain(FLUSH_S)


def hold_desk(settings: Any) -> int | None:
    """One guardian per desk (docs/DESKS.md): a second one for the same mode (a demo guardian started with
    SHORTBOT_MODE=live in .env, say) would race the first over its stops. Held for the process's life; an in-memory
    journal (tests) has no desk to hold."""
    if str(settings.db_path) == ":memory:":
        return None
    run = Path(settings.db_path).parent / "run"
    secure_dir(run)
    fd = os.open(run / f"guardian-{settings.mode}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise SystemExit(f"another guardian already runs the {settings.mode} desk ({run}/guardian-{settings.mode}"
                         ".lock is held): one per mode") from None
    return fd


async def _main() -> None:
    hold_desk(Settings.from_env())
    main_task = asyncio.current_task()
    if main_task:   # `kill` / `docker stop` (SIGTERM) cancel the guardian, so its shutdown runs (#80)
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main_task.cancel)
    try:
        await Guardian().run()
    finally:
        await close_app()


def main() -> None:
    audit.set_process("guardian", claude=False)   # started from a Claude Bash, it still acts on its own (#24)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_log_redaction()   # a traceback of a signed ccxt call carries its URL (#66)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(_main())
    except asyncio.CancelledError:   # SIGTERM, after a clean shutdown: exit as a terminated process does
        raise SystemExit(128 + signal.SIGTERM) from None


if __name__ == "__main__":
    main()

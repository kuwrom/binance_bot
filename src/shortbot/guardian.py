"""Always-on guardian (runs on AWS). It never OPENS trades. It:
- reconciles the journal with the exchange (stop-outs, liquidations, manual closes -> real PnL)
- restores missing stops (force-closes after repeated failures)
- executes the pre-approved parts of each trade's plan (breakeven, trailing, targets/time stop if auto)
- tracks MAE/MFE per trade, alerts on squeeze risk, funding flips, stop proximity, news
- alerts on new listing/delisting announcements; records metrics and labels research candidates
- streams the liquid universe (stream_task.py) and runs the tiered scan on it (scan_tiered.py, docs/SCANNING.md)

Jobs run in lanes (guardian_loop.py) that run concurrently: protective (fills, stops, plans), watch (alerts on
open shorts), monitor (account and market alerts), research, and scan (tiers 1-3 at each 15m close). Every position and every job is isolated and
time-boxed, and a slow or hung job only ever delays its own lane, never the protective one.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from . import audit, procs
from .app import close_app, get_app
from .entries import EntryDesk
from .guardian_jobs import MarketJobs
from .guardian_loop import ESCALATE_AFTER_FAILS, Job, Lane, Streak, send_safe, supervised
from .guardian_margin import MarginChecks
from .guardian_plan import PlanChecks
from .guardian_positions import PositionChecks
from .guardian_watch import WatchChecks
from .killswitch import HEARTBEAT_KEY
from .margin import ADL_PERSIST_PASSES
from .market import streams
from .market.liquidations import LiquidationRecorder, LiquidationStore
from .market.social import SocialStore
from .notify import Notifier
from .redact import install_log_redaction
from .scan_tiered import TieredScan
from .stream_task import StreamTask
from .symlock import GUARDIAN_WAIT_S

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
                       "matched and open orders aren't swept for external ones. Fills, stops and plans ARE still "
                       "checked (the fill poll resolves unknown sends itself): the heartbeat and entries are "
                       "unaffected.",
    "watch": "Squeeze, funding, news and ADL alerts on open shorts are NOT running: watch the positions yourself.",
}
MONITOR = {"governor": (300, 240), "announcements": (180, 240), "social": (3600, 240), "refresh": (3600, 240),
           "housekeeping": (86400, 240), "processes": (3600, 240), "margin_mode": (300, 240),
           "setup_ladder": (86400, 240), "setup_ladder_stops": (600, 120)}   # #69: re-enable check; stops to Telegram
RESEARCH = {"metrics": (1800, 240), "research": (3600, 600), "calendar": (43200, 240), "market_map": (600, 900),
            "sweeps": (86400, 1800), "supply": (86400, 900), "ladder": (900, 240), "laggard": (900, 600),
            "long_shadow": (900, 900)}
SCAN = {"scan": (15, 300)}   # tiers 1-3 (#72): each 15m close, fast events between closes (they decay in 10-45 min)
RESEARCH_START_DELAY_S = 30   # the first protective pass after a (re)start runs on its own
TICK_S = 1.0


class Guardian(PositionChecks, PlanChecks, WatchChecks, MarginChecks, MarketJobs):
    """Dumb and fixed on purpose: uses only the .env baseline limits, never AI research overrides. One exception:
    the kill-switch loss trips (daily loss, dd_stop) and the governor verdict (its halt withdraws resting entries,
    entry_gates.py) follow an active experiment's limits, the ones the session places and sizes under."""

    def __init__(self, app=None):
        self.app = app or get_app(research=False)
        self.n = Notifier(self.app.settings, self.app.journal)
        self.app.trader._alert = self.n.as_trader_alert
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
        self.entries = EntryDesk(self.app.trader)
        self.liq_store = LiquidationStore(self.app.settings.db_path)
        self.social_store = SocialStore(self.app.settings.db_path)
        self._social_task: asyncio.Task | None = None
        self._refresh_proc: asyncio.subprocess.Process | None = None
        self._fund_ts: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []
        self.lanes: dict[str, Lane] = {}
        self.streams_db = streams.path_for(self.app.settings.db_path)   # None for an in-memory journal (tests)
        self.tiered = TieredScan(self.ex, self.j, self.n, self.streams_db, self.app.settings.db_path)

    def _lanes(self) -> dict[str, Lane]:
        fns = {"entries": self.check_entries, "positions": self.check_positions,
               "entry_reconcile": self.reconcile_entries, "watch": self.watch_positions,
               "governor": self.check_governor, "announcements": self.check_announcements,
               "social": self.record_social, "refresh": self.research_refresh, "housekeeping": self.housekeeping,
               "processes": self.check_processes, "margin_mode": self.check_margin_mode,
               "metrics": self.record_metrics, "research": self.label_research, "calendar": self.refresh_calendar,
               "market_map": self.market_map, "sweeps": self.sweep_study, "supply": self.refresh_supply,
               "ladder": self.ladder_shadow, "laggard": self.laggard_study, "long_shadow": self.long_shadow,
               "scan": self.tiered_scan,
               "setup_ladder": self.setup_ladder_check, "setup_ladder_stops": self.forward_ladder_stops}

        def lane(name: str, table: dict, **kw) -> Lane:
            return Lane(name, [Job(k, fns[k], *v) for k, v in table.items()], self.n, self.ex, self.j, TICK_S, **kw)
        return {"protective": lane("protective", PROTECTIVE, escalate_after=ESCALATE_AFTER_FAILS,
                                   escalate_note=FAILING),
                "watch": lane("watch", WATCH, escalate_after=ESCALATE_AFTER_FAILS, escalate_note=FAILING),
                "monitor": lane("monitor", MONITOR), "research": lane("research", RESEARCH),
                "scan": lane("scan", SCAN)}

    def _heartbeat(self, at: float) -> None:
        """Called by a positions pass that verified every held position (or a flat account), `at` = its position read.
        Stamped only while the fill poll's last run succeeded: fills are protected (and journalled) by it. Its age is
        how long protection has gone unverified; every reader acts on that age (killswitch.watch_guardian)."""
        lane = self.lanes.get("protective")
        if lane and lane.failing("entries"):
            log.info("heartbeat withheld: the fill poll is failing")
            return
        try:
            self.j.set(HEARTBEAT_KEY, str(at))
        except Exception:  # noqa: BLE001 - a locked DB must not cost the pass its income read; the age shows it
            log.exception("heartbeat not written")

    # ------------------------------------------------------------ run
    async def _startup(self) -> None:
        await send_safe(self.n, "guardian_started", f"Guardian online ({self.app.settings.mode}). Trading keys: "
                        f"{'yes' if self.ex.trade else 'NO (monitor-only)'}", cooldown_s=0)
        procs.register(self.j, "guardian", self.app.settings.mode)   # #58: the processes job compares its code
        vm = procs.docker_vm(Path(self.app.settings.db_path).expanduser().resolve().parent)
        if vm:
            await send_safe(self.n, "guardian_docker_vm", f"The guardian runs in a container behind a VM: {vm}. flock "
                            "on data/locks and SQLite WAL don't cross the VM boundary, so it is NOT serialised with "
                            "the MCP servers and CLI on the host. Run it natively (deploy/DEPLOY.md section 4).",
                            urgent=True, cooldown_s=0)

    async def run(self) -> None:
        await self._startup()
        self.lanes = self._lanes()
        runs = {name: (lambda ln=ln, d=RESEARCH_START_DELAY_S if name in ("research", "scan") else 0.0: ln.run(d))
                for name, ln in self.lanes.items()}
        # continuous liquidation recording (websocket; the only source of Binance liquidation data)
        runs["liquidations"] = LiquidationRecorder(self.liq_store).run
        if self.streams_db:   # tier 0: market streams into the local store, on its own thread (stream_task.py)
            runs["streams"] = StreamTask(self.ex, self.streams_db).run
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


async def _main() -> None:
    try:
        await Guardian().run()
    finally:
        await close_app()


def main() -> None:
    audit.set_process("guardian", claude=False)   # started from a Claude Bash, it still acts on its own (#24)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_log_redaction()   # a traceback of a signed ccxt call carries its URL (#66)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(_main())


if __name__ == "__main__":
    main()

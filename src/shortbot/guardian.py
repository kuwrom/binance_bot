"""Always-on guardian (runs on AWS). It never OPENS trades. It:
- reconciles the journal with the exchange (stop-outs, liquidations, manual closes -> real PnL)
- restores missing stops (force-closes after repeated failures)
- executes the pre-approved parts of each trade's plan (breakeven, trailing, targets/time stop if auto)
- tracks MAE/MFE per trade, alerts on squeeze risk, funding flips, stop proximity, news
- alerts on new listing/delisting announcements; records metrics and labels research candidates

Jobs run in lanes (guardian_loop.py) that run concurrently: protective (fills, stops, plans), watch (alerts on
open shorts), monitor (account and market alerts) and research. Every position and every job is isolated and
time-boxed, and a slow or hung job only ever delays its own lane, never the protective one.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .app import close_app, get_app
from .entries import EntryDesk
from .guardian_jobs import MarketJobs
from .guardian_loop import ESCALATE_AFTER_FAILS, Job, Lane, Streak, send_safe, supervised
from .guardian_positions import PositionChecks
from .market.liquidations import LiquidationRecorder, LiquidationStore
from .market.social import SocialStore
from .notify import Notifier

log = logging.getLogger("shortbot.guardian")
# job: (every_s, timeout_s). Jobs of one lane run one after another; the lanes run side by side.
PROTECTIVE = {"entries": (5, 240), "positions": (60, 240)}   # streaks escalate; the heartbeat comes from here
WATCH = {"watch": (60, 240)}   # streaks escalate too (silent squeeze checks matter on a short book); no heartbeat
MONITOR = {"governor": (300, 240), "announcements": (180, 240), "social": (3600, 240), "refresh": (3600, 240),
           "housekeeping": (86400, 240)}
RESEARCH = {"metrics": (1800, 240), "research": (3600, 600), "calendar": (43200, 240), "market_map": (600, 900),
            "sweeps": (86400, 1800), "supply": (86400, 900), "ladder": (900, 240), "laggard": (900, 600)}
RESEARCH_START_DELAY_S = 30   # the first protective pass after a (re)start runs on its own
TICK_S = 1.0


class Guardian(PositionChecks, MarketJobs):
    """Dumb and fixed on purpose: uses only the .env baseline limits, never AI research overrides. One exception:
    the kill-switch loss trips (daily loss, dd_stop) follow an active experiment's limits (killswitch.py)."""

    def __init__(self, app=None):
        self.app = app or get_app(research=False)
        self.n = Notifier(self.app.settings, self.app.journal)
        self.app.trader._alert = self.n.as_trader_alert
        self.ex = self.app.ex
        self.j = self.app.journal
        self.stop_fails: dict[str, int] = {}
        self.pos_streak = Streak(ESCALATE_AFTER_FAILS)   # consecutive failed checks per symbol
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

    def _lanes(self) -> dict[str, Lane]:
        fns = {"entries": self.check_entries, "positions": self.check_positions, "watch": self.watch_positions,
               "governor": self.check_governor, "announcements": self.check_announcements,
               "social": self.record_social, "refresh": self.research_refresh, "housekeeping": self.housekeeping,
               "metrics": self.record_metrics, "research": self.label_research, "calendar": self.refresh_calendar,
               "market_map": self.market_map, "sweeps": self.sweep_study, "supply": self.refresh_supply,
               "ladder": self.ladder_shadow, "laggard": self.laggard_study}

        def lane(name: str, table: dict, **kw) -> Lane:
            return Lane(name, [Job(k, fns[k], *v) for k, v in table.items()], self.n, self.ex, self.j, TICK_S, **kw)
        return {"protective": lane("protective", PROTECTIVE, escalate_after=ESCALATE_AFTER_FAILS,
                                   escalate_note="Fills and stops are NOT being checked: check positions and stops "
                                                 "on the exchange.", on_pass=self._heartbeat),
                "watch": lane("watch", WATCH, escalate_after=ESCALATE_AFTER_FAILS,
                              escalate_note="Squeeze, funding and news alerts on open shorts are NOT running: "
                                            "watch the positions yourself."),
                "monitor": lane("monitor", MONITOR), "research": lane("research", RESEARCH)}

    def _heartbeat(self) -> None:
        """Only after a protective pass in which fills and stops were both last checked OK: its age = time since
        protection last worked (the session brief shows it)."""
        self.j.set("guardian_heartbeat", str(time.time()))

    # ------------------------------------------------------------ run
    async def run(self) -> None:
        await send_safe(self.n, "guardian_started", f"Guardian online ({self.app.settings.mode}). Trading keys: "
                        f"{'yes' if self.ex.trade else 'NO (monitor-only)'}", cooldown_s=0)
        self.lanes = self._lanes()
        runs = {name: (lambda ln=ln, d=RESEARCH_START_DELAY_S if name == "research" else 0.0: ln.run(d))
                for name, ln in self.lanes.items()}
        # continuous liquidation recording (websocket; the only source of Binance liquidation data)
        runs["liquidations"] = LiquidationRecorder(self.liq_store).run
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(_main())


if __name__ == "__main__":
    main()

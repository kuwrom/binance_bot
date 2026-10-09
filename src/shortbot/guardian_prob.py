"""Guardian research-lane jobs of the probability layer. Neither touches orders, and both run in the research lane,
apart from the protective one, so a slow fit or a hung read never delays a stop:
- next_level: the advisory next-level mark per open position at each closed 5m bar (exit_marks.py, shadow)
- prob_recal: the daily σ model gate (QLIKE) and σ-scale walk-forward fit (probability/recal.py), off the event loop"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from . import exit_marks
from .probability import recal

if TYPE_CHECKING:
    from .app import App
    from .exchange import Exchanges
    from .journal import Journal

log = logging.getLogger("shortbot.guardian")


class ProbJobs:
    """Guardian mixin; the Guardian attributes it uses are declared below."""
    app: App
    ex: Exchanges
    j: Journal
    _nl_bar: int | None = None

    async def next_level_marks(self) -> None:
        bar = exit_marks.due_bar(int(time.time() * 1000), self._nl_bar)
        if bar is None:
            return
        n = await exit_marks.run_pass(self.ex, self.j, await self.app.trader.positions(), bar)
        self._nl_bar = bar
        log.debug("next-level marks: %s rows for bar %s", n, bar)

    async def prob_recal(self) -> None:
        """The σ model gate (QLIKE: EWMA until HAR wins), then the σ scale for that model."""
        rows = self.j.predictions(labelled_only=True)
        verdict = await asyncio.to_thread(recal.model_verdict, rows)
        res = await asyncio.to_thread(recal.walk_forward, rows, recal.FOLDS, verdict["model"])
        await self.j.write(recal.store_both, verdict, res)
        log.info("σ model %s (%s); scale: applied=%s scale=%s (%s)", verdict["model"], verdict["why"], res["applied"],
                 res["scale"], res["why"])

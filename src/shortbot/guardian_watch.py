"""Guardian watch lane: alert-only checks on open shorts (squeeze, funding, news, ADL) every 60 s. They read public
data and third-party feeds, so they run in their own lane and never delay a stop check. A check that can't get its
inputs is a failure, never a silent skip (#57): alerted, and counted in a per-symbol streak that escalates like the
stop pass."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from . import indicators as ind
from . import news
from .exchange import to_raw
from .guardian_loop import send_safe

if TYPE_CHECKING:
    from .app import App
    from .exchange import Exchanges
    from .guardian_loop import Streak
    from .journal import Journal
    from .notify import Notifier

log = logging.getLogger("shortbot.guardian")
SQUEEZE_PRICE_1H_PCT = 5.0
SQUEEZE_OI_1H_PCT = 5.0
SQUEEZE_BARS = 13   # 5 m candles: one hour
WATCH_ERROR_COOLDOWN_S = 1800
NEWS_DOWN_COOLDOWN_S = 2 * 3600


class WatchInputError(RuntimeError):
    """A watch check's inputs failed or came back short: the checks that needed them did NOT run."""


def _why(v) -> str:
    if isinstance(v, BaseException):
        return f"{type(v).__name__}: {v}"
    return f"only {len(v)} rows" if isinstance(v, list) else f"unexpected {type(v).__name__}"


class WatchChecks:
    """Guardian mixin; the Guardian attributes and MarginChecks methods it uses are declared below."""
    app: App
    ex: Exchanges
    j: Journal
    n: Notifier
    watch_streak: Streak
    adl_streak: Streak

    if TYPE_CHECKING:
        async def adl_read(self) -> tuple[dict[str, int], list[str], str | None]: ...

        async def adl_check(self, p: dict, got: dict[str, int], bad: list[str], err: str | None) -> str | None: ...

    # ------------------------------------------------------------ watch (alert-only, 60 s, own lane)
    async def watch_positions(self) -> None:
        if not self.ex.trade:
            return
        rows = [p for p in await self.app.trader.raw_positions() if p.get("side") == "short"]
        shorts = [p["symbol"] for p in rows]
        adl = await self.adl_read() if rows else ({}, [], None)   # one read for every short (guardian_margin.py)
        for p, sym in zip(rows, shorts, strict=True):
            errors = []
            for name, check in (("squeeze/funding", self._squeeze_and_funding), ("news", self._news)):
                try:   # each on its own: a dead feed never hides a squeeze
                    await check(sym)
                except Exception as e:  # noqa: BLE001 - collected: _watch_result alerts a streak of them
                    log.error("watch %s %s failed: %s", name, sym, e, exc_info=not isinstance(e, WatchInputError))
                    errors.append(f"{name}: {type(e).__name__}: {e}")
            try:
                if why := await self.adl_check(p, *adl):
                    errors.append(why)
            except Exception as e:  # noqa: BLE001 - collected: _watch_result alerts a streak of them
                log.exception("watch adl %s failed", sym)
                errors.append(f"adl: {type(e).__name__}: {e}")
            await self._watch_result(sym, errors)
        for streak in (self.watch_streak, self.adl_streak):   # closed meanwhile
            streak.forget(set(streak.fails) - set(shorts))

    async def _watch_result(self, sym: str, errors: list[str]) -> None:
        """Like the stop pass: each failure alerted (cooldown), one urgent alert per streak, its end announced."""
        if not errors:
            r = self.watch_streak.ok(sym)
            if r:
                await send_safe(self.n, "guardian_recovered:watch", f"watch checks OK again after {r[0]} failures "
                                f"({r[1] / 60:.0f} min)", sym, cooldown_s=0)
            return
        why = "; ".join(errors)
        await send_safe(self.n, "position_watch_error", why, sym, cooldown_s=WATCH_ERROR_COOLDOWN_S)
        if self.watch_streak.fail(sym):
            await send_safe(self.n, "guardian_failing:watch", f"watch failed {self.watch_streak.after}x in a row since "
                            f"{self.watch_streak.started(sym)} (last: {why}). Squeeze, funding and ADL alerts on this "
                            "short are NOT all running: watch it yourself.", sym, urgent=True, cooldown_s=0)

    async def _squeeze_and_funding(self, sym: str) -> None:
        """Every check whose inputs arrived runs; then a failed or short input raises WatchInputError."""
        pub = self.ex.public
        c5, oi, fr, intervals = await asyncio.gather(
            pub.fetch_ohlcv(sym, "5m", limit=SQUEEZE_BARS), self.ex.oi_history(sym, "5m", SQUEEZE_BARS),
            pub.fetch_funding_rate(sym), self.ex.funding_intervals(), return_exceptions=True)
        candles = c5 if isinstance(c5, list) and len(c5) >= SQUEEZE_BARS else None
        oi_rows = oi if isinstance(oi, list) and len(oi) > 1 else None
        rate = fr if isinstance(fr, dict) else None
        ivals = intervals if isinstance(intervals, dict) else None
        bad = {name: _why(v) for name, v, got in (("candles", c5, candles), ("oi", oi, oi_rows),
                                                   ("funding rate", fr, rate), ("funding intervals", intervals, ivals))
               if got is None}
        if candles is not None:
            px = ind.pct_change(candles[0][4], candles[-1][4])
            oi_chg = ind.pct_change(oi_rows[0]["oi"], oi_rows[-1]["oi"]) if oi_rows is not None else 0
            if px >= SQUEEZE_PRICE_1H_PCT and oi_chg >= SQUEEZE_OI_1H_PCT:
                await self.n.send("squeeze_risk", f"Price +{px:.1f}% and OI +{oi_chg:.1f}% in 1h. Re-evaluate now.",
                                  sym, urgent=True, cooldown_s=1800)
            elif px >= SQUEEZE_PRICE_1H_PCT * 1.5:
                await self.n.send("price_spike", f"Price +{px:.1f}% in 1h against the short.", sym, urgent=True,
                                  cooldown_s=1800)
        if rate is not None and (rate.get("fundingRate") or 0) < 0:
            await self.n.send("funding_negative", f"Funding {rate['fundingRate'] * 100:.4f}%: shorts now PAY and the "
                              "trade is crowded. Playbook: consider cutting.", sym, cooldown_s=4 * 3600)
        if ivals is not None and ivals.get(to_raw(sym), {}).get("interval_h") == 1:
            await self.n.send("funding_hourly", "Funding switched to 1h settlement (cap hit): extreme regime.", sym,
                              urgent=True, cooldown_s=4 * 3600)
        if bad:
            raise WatchInputError("; ".join(f"{k}: {v}" for k, v in bad.items()) + " (the checks needing them did "
                                  "NOT run)")

    async def _news(self, sym: str) -> None:
        res = await news.news_for(sym.split("/")[0], max_items=5)
        for item in res["binance_announcements"] + res["headlines"]:
            url, title = item.get("url", ""), item.get("title", "")
            key = self.j.mode_key(f"news_seen:{url or title}")    # a link that failed safe_url() is "": the headline still alerts
            if (url or title) and self.j.get(key) is None:
                self.j.mark(key, str(time.time()))
                await self.n.send("news_on_position", f"{title}\n{url}".strip(), sym, cooldown_s=0)
        if res.get("errors"):   # no news from a dead feed is not "no news" (#66); one alert for every short
            await send_safe(self.n, "news_feed_down", f"news sources failed: {', '.join(res['errors'])}. News on "
                            "open shorts is NOT being watched from them", cooldown_s=NEWS_DOWN_COOLDOWN_S)

"""Session clock (docs/SESSIONS.md): pure functions of a UTC timestamp. Time predicts VOLATILITY and volume
(funding hours, the UTC day open, the US open, Friday 08:00 expiry), not direction, so these feed execution,
stop width and size, plus research features. Hour-of-day return effects don't persist (Baur et al. 2019)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
SESSIONS = (("asia", 0, 8), ("europe", 7, 16), ("us", 13, 21))
FUNDING_GUARD_MIN = 2         # no market entries within ±2 min of a funding timestamp
HOUR_BURST_MIN = 2            # minute :00-:01 of every hour is an algo volatility burst


def session_label(now: datetime) -> str:
    h = now.hour
    names = [n for n, a, b in SESSIONS if a <= h < b]
    return "+".join(names) if names else "late_us"


def session_bucket(now: datetime) -> str:
    """Exclusive bucket for measuring (labels overlap, buckets don't): asia 00-07, london 07-13:30,
    ny 13:30-21 (NY open = 09:30 New York, so it follows US DST), late 21-24 UTC; weekend days are tagged."""
    uo = us_open_utc(now)
    b = ("asia" if now.hour < 7 else "london" if now < uo else "ny" if now.hour < 21 else "late")
    return f"{b}_wknd" if now.weekday() >= 5 else b


def next_funding(now: datetime, interval_h: int = 8) -> datetime:
    """Settlements are on UTC-hour multiples of the symbol's interval (8h default: 00/08/16)."""
    base = now.replace(minute=0, second=0, microsecond=0)
    h = (now.hour // interval_h + 1) * interval_h
    return base.replace(hour=0) + timedelta(hours=h)


def minutes_to_funding(now: datetime, interval_h: int = 8) -> float:
    return (next_funding(now, interval_h) - now).total_seconds() / 60


def minutes_since_funding(now: datetime, interval_h: int = 8) -> float:
    return interval_h * 60 - minutes_to_funding(now, interval_h)


def us_open_utc(now: datetime) -> datetime:
    """09:30 New York on the same calendar day, in UTC (13:30 in DST, 14:30 in winter)."""
    ny = now.astimezone(NY)
    return datetime.combine(ny.date(), time(9, 30), NY).astimezone(UTC)


def minutes_to_day_close(now: datetime) -> float:
    return ((now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) - now).total_seconds() / 60


def minutes_to_week_close(now: datetime) -> float:
    days = (7 - now.weekday()) % 7 or 7
    return ((now + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0) - now).total_seconds() / 60


def execution_guard(now: datetime, interval_h: int = 8) -> str | None:
    """Reason not to send a MARKET order this minute (resting limits are fine)."""
    to_f, since_f = minutes_to_funding(now, interval_h), minutes_since_funding(now, interval_h)
    if to_f <= FUNDING_GUARD_MIN or since_f <= FUNDING_GUARD_MIN:
        return f"within {FUNDING_GUARD_MIN} min of a funding settlement: spreads and volatility spike, wait"
    if now.minute < HOUR_BURST_MIN:
        return "top-of-hour algo volatility burst (minute :00-:01): wait a minute"
    return None


def clock(now: datetime | None = None, interval_h: int = 8) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    uo = us_open_utc(now)
    if (uo - now).total_seconds() < -30 * 60:   # today's open is over: count to the next one
        uo = us_open_utc(now + timedelta(days=1))
    to_uo = (uo - now).total_seconds() / 60
    soft = []
    if now.hour in (0, 16):
        soft.append(f"{now.hour:02d} UTC is a peak-volatility hour (funding / day open)")
    if now.weekday() == 4 and 7 <= now.hour < 9:
        soft.append("Friday 08:00 UTC Deribit expiry + funding")
    if minutes_to_week_close(now) <= 120:
        soft.append("weekly close (Monday 00 UTC) within 2h")
    if abs(to_uo) <= 30 and now.weekday() < 5:
        soft.append("US equity open ±30 min: volatility and BTC beta rise")
    return {
        "utc": now.isoformat(timespec="minutes"), "utc_hour": now.hour, "weekday": now.strftime("%a"),
        "weekend": now.weekday() >= 5, "session": session_label(now), "session_bucket": session_bucket(now),
        "minute_of_hour": now.minute,
        "funding_interval_h": interval_h, "min_to_funding": round(minutes_to_funding(now, interval_h), 1),
        "min_to_day_close": round(minutes_to_day_close(now)), "min_to_week_close": round(minutes_to_week_close(now)),
        "min_to_us_open": round(to_uo), "execution_guard": execution_guard(now, interval_h),
        "soft_warnings": soft,
    }

"""Settings loaded from environment (.env).

Risk limits come in layers (resolved in desk_limits.py; see params.py):
- built-in defaults, then `.env` (`RiskLimits.from_env()`, `Settings.limits`), then the human's per-desk limits in the
  journal (`shortbot-admin limits`): together the desk's BASELINE. The always-on guardian (AWS) only uses this.
- The AI analyst may override any RiskLimits field for research (e.g. try 5x leverage), but only
  inside `ResearchBounds`, an outer envelope that only the human changes (BOUND_* in .env).
Every opening order is still checked by risk.check_open_short against the effective limits.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

NO_DOTENV = "SHORTBOT_NO_DOTENV"   # set (the test suite sets it before importing shortbot): .env is never loaded


def load_env() -> bool:
    """The checkout's .env into os.environ, once, at import; variables already set win. python-dotenv finds it by
    walking up from this file, not from the working directory, so any process importing shortbot from the checkout
    loads it: the guardian, MCP servers, the CLI, and every module that reads os.getenv (provenance, backup) after
    this import. Skipped under NO_DOTENV, which the processes a test run starts inherit too (#80)."""
    return False if os.getenv(NO_DOTENV) else load_dotenv()


load_env()

LIVE_CONFIRM_PHRASE = "I_ACCEPT_REAL_MONEY_RISK"


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _opt(name: str) -> float | None:
    """An optional limit: unset or empty = None (disabled)."""
    v = (os.getenv(name) or "").strip()
    return float(v) if v else None


@dataclass(frozen=True)
class RiskLimits:
    max_leverage: int = 3
    max_position_notional: float = 100.0      # USDT per coin
    max_total_notional: float = 400.0         # USDT across all shorts
    max_positions: int = 5
    risk_per_trade_pct: float = 1.0           # max loss at stop, % of equity
    daily_loss_limit_pct: float = 3.0         # blocks new entries for the UTC day
    min_stop_distance_pct: float = 0.5
    max_stop_distance_pct: float = 25.0
    min_quote_volume_24h: float = 20_000_000  # USDT liquidity floor
    liq_buffer_pct: float = 2.0               # stop must trigger this many % before est. liquidation
    max_drawdown_pct: float = 20.0            # risk scales down linearly to 25% as drawdown -> this (m_dd)
    max_portfolio_heat_pct: float = 5.0       # total open risk at stops, % of equity
    max_cluster_heat_pct: float = 3.5         # correlated alt shorts count as one cluster
    # governor.py breakers (docs/RISK_GOVERNANCE.md)
    weekly_loss_limit_pct: float = 4.0        # halt until Monday 00:00 UTC
    dd_halve_pct: float = 8.0                 # drawdown from peak -> half size
    dd_stop_pct: float = 12.0                 # drawdown from peak -> halt until a human review
    loss_streak_halve: int = 3                # consecutive losses -> half size for the UTC day / until 2 wins
    loss_streak_pause: int = 5                # consecutive losses -> 24h cool-off
    giveback_start_pct: float = 1.0           # day peak that arms the give-back stop
    giveback_keep_frac: float = 0.5           # keep at least this fraction of the day's peak
    moonshot_bucket_pct: float = 5.0          # ring-fenced equity for "M" bets; 25% of it at risk per bet
    max_conviction_mult: float = 1.5          # cap on conviction x cell tilt
    max_add_r_mult: float = 1.0               # adds to a winner may raise the loss at stop to this x the pinned 1R
    min_add_r: float = 1.0                    # pyramiding: add only once the open profit is >= this x the pinned 1R
    max_add_frac: float = 0.5                 # each add <= this fraction of the initial size (plan.full_size)
    max_adds: int = 2                         # adds per trade (add_to_winner)
    # equity-relative size caps (None = off): the per-coin / total notional cap is min(USDT cap, x * equity)
    max_position_equity_x: float | None = None
    max_total_equity_x: float | None = None

    @classmethod
    def from_env(cls) -> RiskLimits:
        return cls(
            max_leverage=_i("MAX_LEVERAGE", cls.max_leverage),
            max_position_notional=_f("MAX_POSITION_NOTIONAL", cls.max_position_notional),
            max_total_notional=_f("MAX_TOTAL_NOTIONAL", cls.max_total_notional),
            max_positions=_i("MAX_POSITIONS", cls.max_positions),
            risk_per_trade_pct=_f("RISK_PER_TRADE_PCT", cls.risk_per_trade_pct),
            daily_loss_limit_pct=_f("DAILY_LOSS_LIMIT_PCT", cls.daily_loss_limit_pct),
            min_stop_distance_pct=_f("MIN_STOP_DISTANCE_PCT", cls.min_stop_distance_pct),
            max_stop_distance_pct=_f("MAX_STOP_DISTANCE_PCT", cls.max_stop_distance_pct),
            min_quote_volume_24h=_f("MIN_QUOTE_VOLUME_24H", cls.min_quote_volume_24h),
            liq_buffer_pct=_f("LIQ_BUFFER_PCT", cls.liq_buffer_pct),
            max_drawdown_pct=_f("MAX_DRAWDOWN_PCT", cls.max_drawdown_pct),
            max_portfolio_heat_pct=_f("MAX_PORTFOLIO_HEAT_PCT", cls.max_portfolio_heat_pct),
            max_cluster_heat_pct=_f("MAX_CLUSTER_HEAT_PCT", cls.max_cluster_heat_pct),
            weekly_loss_limit_pct=_f("WEEKLY_LOSS_LIMIT_PCT", cls.weekly_loss_limit_pct),
            dd_halve_pct=_f("DD_HALVE_PCT", cls.dd_halve_pct),
            dd_stop_pct=_f("DD_STOP_PCT", cls.dd_stop_pct),
            loss_streak_halve=_i("LOSS_STREAK_HALVE", cls.loss_streak_halve),
            loss_streak_pause=_i("LOSS_STREAK_PAUSE", cls.loss_streak_pause),
            giveback_start_pct=_f("GIVEBACK_START_PCT", cls.giveback_start_pct),
            giveback_keep_frac=_f("GIVEBACK_KEEP_FRAC", cls.giveback_keep_frac),
            moonshot_bucket_pct=_f("MOONSHOT_BUCKET_PCT", cls.moonshot_bucket_pct),
            max_conviction_mult=_f("MAX_CONVICTION_MULT", cls.max_conviction_mult),
            max_add_r_mult=_f("MAX_ADD_R_MULT", cls.max_add_r_mult),
            min_add_r=_f("MIN_ADD_R", cls.min_add_r),
            max_add_frac=_f("MAX_ADD_FRAC", cls.max_add_frac),
            max_adds=_i("MAX_ADDS", cls.max_adds),
            max_position_equity_x=_opt("MAX_POSITION_EQUITY_X"),
            max_total_equity_x=_opt("MAX_TOTAL_EQUITY_X"),
        )

    def position_cap(self, equity: float | None) -> float:
        """Per-coin notional cap: max_position_notional, or x * equity when max_position_equity_x is set and lower."""
        return _cap(self.max_position_notional, self.max_position_equity_x, equity)

    def total_cap(self, equity: float | None) -> float:
        """Total open notional cap: max_total_notional, or x * equity when max_total_equity_x is set and lower."""
        return _cap(self.max_total_notional, self.max_total_equity_x, equity)


def _cap(fixed: float, x: float | None, equity: float | None) -> float:
    return min(fixed, x * equity) if x and equity and equity > 0 else fixed


def consistency_errors(lim: RiskLimits) -> list[str]:
    """Combinations of limits that make no sense together (research overrides and desk limits are both checked)."""
    errs = []
    if lim.min_stop_distance_pct >= lim.max_stop_distance_pct:
        errs.append("min_stop_distance_pct must be < max_stop_distance_pct")
    if lim.dd_halve_pct >= lim.dd_stop_pct:
        errs.append("dd_halve_pct must be < dd_stop_pct")
    if lim.loss_streak_halve >= lim.loss_streak_pause:
        errs.append("loss_streak_halve must be < loss_streak_pause")
    if lim.max_position_notional > lim.max_total_notional:
        errs.append("max_position_notional must be <= max_total_notional")
    px, tx = lim.max_position_equity_x, lim.max_total_equity_x
    if px is not None and tx is not None and px > tx:
        errs.append("max_position_equity_x must be <= max_total_equity_x")
    return errs


# Outer envelope for AI research overrides: (low, high) per RiskLimits field. Human-only, via
# BOUND_<FIELD>_MIN / BOUND_<FIELD>_MAX in .env, e.g. BOUND_MAX_LEVERAGE_MAX=10.
DEFAULT_BOUNDS: dict[str, tuple[float, float]] = {
    "max_leverage": (1, 10),
    "max_position_notional": (5, 1000),
    "max_total_notional": (5, 4000),
    "max_positions": (1, 10),
    "risk_per_trade_pct": (0.1, 3.0),
    "daily_loss_limit_pct": (0.5, 10.0),
    "min_stop_distance_pct": (0.1, 5.0),
    "max_stop_distance_pct": (1.0, 50.0),
    "min_quote_volume_24h": (1_000_000, 1_000_000_000),
    "liq_buffer_pct": (0.5, 20.0),
    "max_drawdown_pct": (5.0, 40.0),
    "max_portfolio_heat_pct": (0.5, 15.0),
    "max_cluster_heat_pct": (0.5, 10.0),
    "weekly_loss_limit_pct": (1.0, 20.0),
    "dd_halve_pct": (2.0, 30.0),
    "dd_stop_pct": (4.0, 40.0),
    "loss_streak_halve": (2, 10),
    "loss_streak_pause": (3, 15),
    "giveback_start_pct": (0.25, 10.0),
    "giveback_keep_frac": (0.0, 0.9),
    "moonshot_bucket_pct": (0.0, 10.0),
    "max_conviction_mult": (1.0, 2.0),
    "max_add_r_mult": (1.0, 2.0),
    "min_add_r": (0.0, 5.0),
    "max_add_frac": (0.1, 1.0),
    "max_adds": (0, 10),
    "max_position_equity_x": (0.1, 20.0),
    "max_total_equity_x": (0.1, 20.0),
}


@dataclass(frozen=True)
class ResearchBounds:
    ranges: dict[str, tuple[float, float]] = field(default_factory=lambda: dict(DEFAULT_BOUNDS))

    def get(self, name: str) -> tuple[float, float]:
        return self.ranges[name]

    def as_dict(self) -> dict[str, list[float]]:
        return {k: [lo, hi] for k, (lo, hi) in self.ranges.items()}

    @classmethod
    def from_env(cls) -> ResearchBounds:
        out = {}
        for k, (lo, hi) in DEFAULT_BOUNDS.items():
            out[k] = (_f(f"BOUND_{k.upper()}_MIN", lo), _f(f"BOUND_{k.upper()}_MAX", hi))
        return cls(out)


@dataclass(frozen=True)
class Settings:
    mode: str                      # "demo" or "live"
    api_key: str
    api_secret: str
    db_path: Path
    telegram_token: str
    telegram_chat_id: str
    limits: RiskLimits = field(default_factory=RiskLimits)     # defaults + .env; desk_limits.baseline adds the desk's
    bounds: ResearchBounds = field(default_factory=ResearchBounds)
    allow_ai_params_live: bool = False                         # AI overrides in live mode? human-only
    # SHORTBOT_GATEWAY (#36, GATEWAY.md §3; gateway_flag): sessions / the guardian send order writes to shortbot-gateway.
    # From the environment both default on; a Settings built directly (tests, tools) writes in-process unless told.
    gateway: bool = False
    gateway_guardian: bool = False
    invalid: tuple[str, ...] = ()   # settings that fell back to their default (the guardian alerts config_invalid)
    guardian_market: bool = True   # SHORTBOT_GUARDIAN_MARKET=0: this guardian runs only its account's jobs (DESKS.md)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @classmethod
    def from_env(cls) -> Settings:
        mode = os.getenv("SHORTBOT_MODE", "demo").lower()
        if mode not in ("demo", "live"):
            raise ValueError(f"SHORTBOT_MODE must be demo or live, got {mode!r}")
        if mode == "live" and os.getenv("LIVE_CONFIRM") != LIVE_CONFIRM_PHRASE:
            raise ValueError(f"Live mode requires LIVE_CONFIRM={LIVE_CONFIRM_PHRASE}")
        prefix = "LIVE" if mode == "live" else "DEMO"
        sessions, guardian, bad = gateway_flag(os.getenv("SHORTBOT_GATEWAY"))
        return cls(
            mode=mode,
            api_key=os.getenv(f"BINANCE_{prefix}_API_KEY", ""),
            api_secret=os.getenv(f"BINANCE_{prefix}_API_SECRET", ""),
            db_path=Path(os.getenv("SHORTBOT_DB", "data/shortbot.db")),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            limits=RiskLimits.from_env(),
            bounds=ResearchBounds.from_env(),
            allow_ai_params_live=os.getenv("AI_PARAMS_IN_LIVE", "false").lower() == "true",
            gateway=sessions, gateway_guardian=guardian, invalid=(bad,) if bad else (),
            guardian_market=os.getenv("SHORTBOT_GUARDIAN_MARKET", "1").strip().lower() not in ("0", "false", "no", "off"),
        )


GATEWAY_ON, GATEWAY_SESSIONS, GATEWAY_OFF = ("", "1", "true", "yes", "on"), ("sessions",), ("0", "false", "no", "off")


def gateway_flag(raw: str | None) -> tuple[bool, bool, str | None]:
    """SHORTBOT_GATEWAY -> (sessions use the gateway, the guardian does, why the value was not understood).
    Unset or on: both (#36 step 5-6). `sessions`: the sessions only (the guardian writes in-process: step 6's rollback).
    0/off: neither (step 5's rollback). Anything else is the default, never an error: a typo in .env must not stop
    the guardian from starting (positions would go unprotected); it is logged here and alerted by the guardian."""
    v = (raw or "").strip().lower()
    if v in GATEWAY_ON or v in GATEWAY_SESSIONS or v in GATEWAY_OFF:
        return v not in GATEWAY_OFF, v in GATEWAY_ON, None
    why = (f"SHORTBOT_GATEWAY={raw!r} is not one of 1/on, sessions, 0/off: using the default (on: sessions and the "
           "guardian go through the order gateway)")
    logging.getLogger("shortbot.config").warning("%s", why)
    return True, True, why

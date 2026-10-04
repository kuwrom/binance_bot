"""Desk limits: the human's risk limits per desk (demo / live), in the journal (owner decision 2026-10-04).

Every baseline is resolved here, in this order (later wins):
1. built-in defaults (`config.RiskLimits`);
2. `.env` (`RiskLimits.from_env()` = `Settings.limits`): still works unchanged when no desk limits are set;
3. desk limits (this module): kv `desk_limits`, per mode (Journal.mode_key: demo the bare key, live `@live`);
4. research overrides (params.py): demo only (live only with AI_PARAMS_IN_LIVE=true), bounded by BOUND_*.
1-3 are the desk's BASELINE: what the guardian, its kill-switch loss trips and governor, and every placement check
see without research overrides. Read on every use (one kv row), so a change reaches the guardian without a restart.

Only `shortbot-admin limits` writes them (admin_limits.py): nothing in the MCP server or the `shortbot` CLI may, so
the AI can't raise them (tests/test_desk_limits_writers.py). A stored value that no longer validates is ignored
(the layer below applies) and logged, never a crash in the guardian.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, fields, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from .config import RiskLimits, Settings, consistency_errors

if TYPE_CHECKING:
    from .journal import Journal
    from .params import ResearchParams

log = logging.getLogger("shortbot.desk_limits")

KV = "desk_limits"            # {field: value}, per mode; written by shortbot-admin only
FIELDS = tuple(f.name for f in fields(RiskLimits))
INT_FIELDS = frozenset(f.name for f in fields(RiskLimits) if f.type in (int, "int"))
ZERO_OK = frozenset({"giveback_keep_frac", "moonshot_bucket_pct", "min_add_r", "max_adds"})   # 0 is meaningful
EQUITY_X = frozenset({"max_position_equity_x", "max_total_equity_x"})
EQUITY_X_MAX = 20.0


class LimitError(ValueError):
    pass


def env_name(name: str) -> str:
    return name.upper()


def key(j: Journal, mode: str | None = None) -> str:
    """The kv key of `mode`'s desk limits (default: the journal's own mode), via Journal.mode_key."""
    from .journal import Journal
    return Journal.mode_key(SimpleNamespace(mode=mode or j.mode), KV)


def field_name(raw: str) -> str:
    """A field name or its env-var name, any case -> the RiskLimits field."""
    name = raw.strip().lower()
    if name not in FIELDS:
        raise LimitError(f"unknown limit {raw!r}; known: {', '.join(FIELDS)}")
    return name


def coerce(name: str, raw: Any) -> int | float:
    """`raw` as the field's type, sanity-checked: positive (0 where it means something), leverage >= 1, pct fields in
    (0, 100], equity multiples in (0, 20]. Raises LimitError."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise LimitError(f"{name}: not a number ({raw!r})") from None
    if v != v or v in (float("inf"), float("-inf")):
        raise LimitError(f"{name}: not a finite number ({raw!r})")
    if name in INT_FIELDS:
        if v != int(v):
            raise LimitError(f"{name}: must be a whole number ({raw!r})")
        v = int(v)
    if v < 0 or (v == 0 and name not in ZERO_OK):
        raise LimitError(f"{name}: must be positive ({raw!r})")
    if name == "max_leverage" and v < 1:
        raise LimitError(f"{name}: must be >= 1 ({raw!r})")
    if name.endswith("_pct") and v > 100:
        raise LimitError(f"{name}: a percentage must be <= 100 ({raw!r})")
    if name.endswith("_frac") and v > 1:
        raise LimitError(f"{name}: a fraction must be <= 1 ({raw!r})")
    if name in EQUITY_X and v > EQUITY_X_MAX:
        raise LimitError(f"{name}: must be <= {EQUITY_X_MAX:g} x equity ({raw!r})")
    return v


def _raw(j: Journal, mode: str | None) -> dict[str, Any]:
    try:
        raw = j.get(key(j, mode))
        data = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001 - an unreadable row never stops the guardian: the .env layer applies
        log.exception("desk limits unreadable (%s): using .env", mode or j.mode)
        return {}
    return data if isinstance(data, dict) else {}


def stored(j: Journal | None, mode: str | None = None) -> dict[str, int | float]:
    """The desk's valid stored limits (an invalid entry is skipped and logged)."""
    if j is None:
        return {}
    out: dict[str, int | float] = {}
    for k, v in _raw(j, mode).items():
        try:
            out[field_name(k)] = coerce(field_name(k), v)
        except LimitError as e:
            log.warning("desk limit ignored (%s): %s", mode or j.mode, e)
    return out


def baseline(settings: Settings, j: Journal | None, mode: str | None = None) -> RiskLimits:
    """defaults -> .env -> desk limits: the baseline the guardian and every check without research overrides use.
    `mode`: another desk's (the go-live report compares demo trades with the live desk's baseline)."""
    desk = stored(j, mode)
    return replace(settings.limits, **desk) if desk else settings.limits


def env_layer_source(name: str) -> str:
    return ".env" if (os.getenv(env_name(name)) or "").strip() else "default"


def describe(settings: Settings, j: Journal, params: ResearchParams | None = None) -> dict[str, Any]:
    """Per field: the effective value and its source (default / .env / desk / research), plus each layer's value."""
    desk = stored(j)
    research = params.overrides() if params is not None and params.active else {}
    eff = replace(baseline(settings, j), **research) if research else baseline(settings, j)
    rows: dict[str, Any] = {}
    for f in FIELDS:
        src = "research" if f in research else "desk" if f in desk else env_layer_source(f)
        rows[f] = {"value": getattr(eff, f), "source": src, "env": getattr(settings.limits, f), "env_var": env_name(f),
                   **({"desk": desk[f]} if f in desk else {}), **({"research": research[f]} if f in research else {})}
    return {"mode": settings.mode, "fields": rows, "desk": desk, "effective": asdict(eff)}


def brief_line(settings: Settings, j: Journal) -> str:
    """One line for the brief: the human's desk limits on this desk, or that the .env baseline applies."""
    desk = stored(j)
    if not desk:
        return f"- desk limits ({settings.mode}): none set, the .env baseline applies (human: shortbot-admin limits)"
    parts = ", ".join(f"{k} {v:g}" for k, v in sorted(desk.items()))
    return f"- desk limits ({settings.mode}, human-set, override .env): {parts}"


def validate_changes(settings: Settings, j: Journal, changes: dict[str, Any], drop: tuple[str, ...] = ()
                     ) -> tuple[dict[str, int | float], dict[str, int | float], list[str]]:
    """(current desk limits, new desk limits, errors) for setting `changes` and dropping `drop`."""
    cur, errs, new = stored(j), [], dict(stored(j))
    for raw_k, raw_v in changes.items():
        try:
            k = field_name(raw_k)
            new[k] = coerce(k, raw_v)
        except LimitError as e:
            errs.append(str(e))
    for raw_k in drop:
        try:
            new.pop(field_name(raw_k), None)
        except LimitError as e:
            errs.append(str(e))
    if not errs:
        errs += consistency_errors(replace(settings.limits, **new))
    return cur, new, errs


def write(j: Journal, new: dict[str, int | float], shown: str | None, mode: str | None = None) -> bool:
    """Compare-and-set the desk's limits: only if the row is still `shown` (what the human confirmed). Called only from
    admin_limits.py (shortbot-admin)."""
    k = key(j, mode)

    def swap(j: Journal) -> bool:
        if j.get(k) != shown:
            return False
        j.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(new, sort_keys=True)))
        return True
    return j.run(swap)


__all__ = ["KV", "LimitError", "baseline", "describe", "stored", "brief_line", "validate_changes", "write",
           "field_name", "coerce", "key"]

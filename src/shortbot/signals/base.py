"""Signal plug-in framework.

A signal is an async function `ctx -> SignalOut` registered with `@signal(...)`. It reads data only via the
MarketContext and returns:
  features  - named values (logged to research automatically; setups/tape/playbooks read them)
  points    - score contributions with a human reason (the short-setup score is their sum, capped at 100)
  vetoes    - reasons NOT to short now

Groups let the tape classifier / callers run only what matters ("core" always runs):
  core   - basic market state (price change, liquidity, listing) - always on
  froth  - overextension (funding, OI, RSI, VWAP stretch, volume)
  trigger- confirmation (lower highs, taker flip, rolling over)
  tape   - order flow / microstructure (CVD, sweeps, spot vs perp, book, liquidations)
  social - attention (trending, hype, Square mention velocity)
  event  - scheduled supply events (unlocks, delistings)
  guard  - vetoes only (data completeness, majors, crowding)

Add a signal: write one function in a module under signals/, decorate it, import the module in
signals/__init__.py. Nothing else changes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..market import MarketContext

GROUPS = ("core", "froth", "trigger", "tape", "social", "event", "guard")


@dataclass
class SignalOut:
    features: dict[str, Any] = field(default_factory=dict)
    points: list[tuple[int, str]] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SignalSpec:
    name: str
    group: str
    fn: Callable[[MarketContext], Awaitable[SignalOut]]
    doc: str
    default_on: bool = True


REGISTRY: dict[str, SignalSpec] = {}


def signal(name: str, group: str, default_on: bool = True):
    if group not in GROUPS:
        raise ValueError(f"group must be one of {GROUPS}")

    def deco(fn):
        if name in REGISTRY:
            raise ValueError(f"duplicate signal {name!r}")
        REGISTRY[name] = SignalSpec(name, group, fn, (fn.__doc__ or "").strip(), default_on)
        return fn
    return deco


@dataclass
class Evaluation:
    symbol: str
    features: dict[str, Any]
    score: int
    signals: list[str]
    vetoes: list[str]
    errors: dict[str, str]

    @property
    def tradable(self) -> bool:
        return not self.vetoes


def select(groups: tuple[str, ...] | None = None, names: tuple[str, ...] | None = None) -> list[SignalSpec]:
    specs = [s for s in REGISTRY.values() if s.default_on or (names and s.name in names)]
    if groups:
        specs = [s for s in specs if s.group in groups or s.group == "core"]
    if names:
        specs = [s for s in specs if s.name in names or s.group == "core"]
    return specs


async def evaluate(ctx: MarketContext, groups: tuple[str, ...] | None = None,
                   names: tuple[str, ...] | None = None) -> Evaluation:
    """Run the selected signals concurrently on one context and merge their outputs.
    `guard` signals run last because they may read other signals' features and the context's missing data."""
    specs = select(groups, names)
    first = [s for s in specs if s.group != "guard"]
    guards = [s for s in specs if s.group == "guard"]
    outs: dict[str, SignalOut] = {}
    errors: dict[str, str] = {}

    async def run(spec: SignalSpec, features: dict[str, Any] | None = None) -> None:
        try:
            if features is not None:
                ctx.features = features  # guards read the merged features of the first wave
            outs[spec.name] = await spec.fn(ctx)
        except Exception as e:  # noqa: BLE001 - a broken signal must not break the scan
            errors[spec.name] = f"{type(e).__name__}: {e}"

    await asyncio.gather(*(run(s) for s in first))
    merged: dict[str, Any] = {"symbol": ctx.symbol}
    for o in outs.values():
        merged.update(o.features)
    for g in guards:
        await run(g, dict(merged))
        merged.update(outs.get(g.name, SignalOut()).features)
    ordered = [outs[s.name] for s in specs if s.name in outs]  # registry order: deterministic output
    points = [p for o in ordered for p in o.points]
    vetoes = [v for o in ordered for v in o.vetoes]
    total = min(sum(p for p, _ in points), 100)
    merged["score"] = total
    merged["data_missing"] = sorted(ctx.missing)
    return Evaluation(ctx.symbol, merged, total, [f"+{p} {why}" for p, why in points], vetoes, errors)

"""Signal plug-ins. Importing a module registers its signals; list them here so they are always loaded."""

from . import controller, core, depth, events, lifecycle, robust, sector, session, social, tape  # noqa: F401  (importing registers the signals)
from .base import REGISTRY, Evaluation, SignalOut, evaluate, select, signal

__all__ = ["REGISTRY", "Evaluation", "SignalOut", "evaluate", "select", "signal"]


def describe() -> list[dict]:
    return [{"name": s.name, "group": s.group, "default_on": s.default_on, "doc": s.doc} for s in REGISTRY.values()]

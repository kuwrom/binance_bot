"""Signal plug-ins. Importing a module registers its signals; list them here so they are always loaded."""

from . import (  # noqa: F401, I001  (importing registers the signals, in this order: REGISTRY's)
    controller, core, depth, events, hack, lifecycle, onchain, robust, sector, session, social, supply, tape)
from .base import REGISTRY, Evaluation, evaluate

__all__ = ["REGISTRY", "Evaluation", "evaluate"]   # via the package, so every plug-in above is registered


def describe() -> list[dict]:
    return [{"name": s.name, "group": s.group, "default_on": s.default_on, "doc": s.doc} for s in REGISTRY.values()]

"""The guardian's side inside the order gateway (#36 step 6, docs/GATEWAY.md §5): the context its position-pass intents
run with there (GuardianActs), and the start-up reconciliation the gateway runs before it listens."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .guardian_plan import PlanChecks

if TYPE_CHECKING:
    from .app import App
    from .notify import Notifier


class GuardianActs(PlanChecks):
    """The ctx of restore_stop, force_close, cap_stop, pull_targets and plan_step when the gateway runs them
    (gateway.CTX "guardian"): what those handlers use of the Guardian. The app is the gateway's own (one Trader); the
    intent runs for the guardian (origin.guardian), so the Trader sees the .env baseline and alerts through `n`."""

    def __init__(self, app: App, n: Notifier):
        self.app, self.j, self.n = app, app.journal, n
